// 貸借対照表 (B/S) のクライアント描画。
// MK 復号→ min_year..year を fetch して結合 → 集計 → DOM 構築まで完結。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _fmtYen(n) {
  return "¥" + (n || 0).toLocaleString();
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("bs-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("bs-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("balance_sheet_renderer: " + msg, logArg);
  _setStatus("貸借対照表の初期化に失敗しました: " + msg, "danger");
}


function _renderSection(tbodyId, rows, total, totalLabel, emptyMsgId, niRow) {
  const tbody = document.getElementById(tbodyId);
  const emptyMsg = document.getElementById(emptyMsgId);
  const totalEl = document.getElementById(tbodyId + "-total");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

  const hasData = rows.length > 0 || !!niRow;
  if (emptyMsg) emptyMsg.classList.toggle("d-none", hasData);
  if (tbody.parentElement) {
    tbody.parentElement.classList.toggle("d-none", !hasData);
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.setAttribute("data-bs-row", row.account_code);
    const nameTd = document.createElement("td");
    nameTd.textContent = row.account_name;
    const amountTd = document.createElement("td");
    amountTd.className = "text-end";
    amountTd.textContent = _fmtYen(row.balance);
    tr.appendChild(nameTd);
    tr.appendChild(amountTd);
    tbody.appendChild(tr);
  }
  if (niRow) {
    const tr = document.createElement("tr");
    tr.className = "text-muted fst-italic";
    const nameTd = document.createElement("td");
    nameTd.textContent = "（当期純利益）";
    const amountTd = document.createElement("td");
    amountTd.className = "text-end";
    amountTd.textContent = _fmtYen(niRow.balance);
    tr.appendChild(nameTd);
    tr.appendChild(amountTd);
    tbody.appendChild(tr);
  }

  if (totalEl) totalEl.textContent = _fmtYen(total);
}


function _renderClosingHint(hasClosing) {
  const hint = document.getElementById("bs-closing-hint");
  if (!hint) return;
  hint.classList.toggle("d-none", hasClosing);
}


function _renderBalanceCheck(totals) {
  const card = document.getElementById("bs-balance-card");
  const lhsEl = document.getElementById("bs-balance-lhs");
  const rhsEl = document.getElementById("bs-balance-rhs");
  const iconEl = document.getElementById("bs-balance-icon");
  const diffEl = document.getElementById("bs-balance-diff");
  if (!card) return;
  card.classList.remove("text-bg-success", "text-bg-warning");
  card.classList.add(totals.is_balanced ? "text-bg-success" : "text-bg-warning");
  if (lhsEl) lhsEl.textContent = _fmtYen(totals.assets);
  if (rhsEl) rhsEl.textContent = _fmtYen(totals.liabilities + totals.equity_with_ni);
  if (iconEl) {
    iconEl.className = totals.is_balanced
      ? "bi bi-check-circle ms-1"
      : "bi bi-exclamation-triangle ms-1";
  }
  if (diffEl) {
    if (totals.is_balanced) {
      diffEl.textContent = "";
      diffEl.classList.add("d-none");
    } else {
      diffEl.textContent = " 差額: " + _fmtYen(totals.diff);
      diffEl.classList.remove("d-none");
    }
  }
}


function _renderView(view) {
  _renderClosingHint(view.has_closing);
  _renderSection(
    "bs-assets-tbody", view.sections.assets.rows, view.sections.assets.total,
    "資産合計", "bs-assets-empty", null,
  );
  _renderSection(
    "bs-liabilities-tbody", view.sections.liabilities.rows,
    view.sections.liabilities.total, "負債合計", "bs-liabilities-empty", null,
  );
  _renderSection(
    "bs-equities-tbody", view.sections.equities.rows,
    view.sections.equities.total, "純資産合計", "bs-equities-empty",
    view.sections.equities.net_income_row,
  );
  _renderBalanceCheck(view.totals);
}


async function _run() {
  const paramsEl = document.getElementById("bs-server-params");
  if (!paramsEl) { _abort("server params script not found"); return; }
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    _abort("failed to parse params", e);
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number") {
    _abort("invalid params (user_id/year)");
    return;
  }

  const accountsEl = document.getElementById("bs-accounts-meta");
  if (!accountsEl) { _abort("accounts meta script not found"); return; }
  let accountsMeta;
  try {
    accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    _abort("failed to parse accounts meta", e);
    return;
  }

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { computeBalanceSheet },
      { composeBalanceSheetView },
      { fetchBalanceCacheBlobs },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/balance_sheet.js"),
      import(getStaticRoot() + "js/crypto/reports/balance_sheet_view.js"),
      import(getStaticRoot() + "js/crypto/balance_cache_blobs_client.js"),
    ]);

    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      _setStatus(
        "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
        "warning",
      );
      return;
    }
    if (params.is_audit_proxy) {
      _setStatus(
        "監査代理閲覧中です。オーナーの暗号化された仕訳はあなたの暗号鍵では復号できないため、貸借対照表は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    // 仕訳ゼロ件なら min_year は null。空 view を描画して終わり
    if (params.min_year == null) {
      _renderView(composeBalanceSheetView({
        assets: [], liabilities: [], equities: [],
        total_assets: 0, total_liabilities: 0, total_equity: 0,
        net_income: 0, has_closing: false,
      }));
      return;
    }

    const accountTypeByCode = {};
    const normalBalanceByCode = {};
    const accountNameByCode = {};
    for (const [code, meta] of Object.entries(accountsMeta)) {
      accountTypeByCode[code] = meta.type;
      normalBalanceByCode[code] = meta.normal_balance;
      accountNameByCode[code] = meta.name;
    }

    // BCB 統合 (#221): min_year..year-1 の順次 fetch を「前年 BCB period=15」
    // 1 リクエストに置換。前年末累計を priorCumulative に流し、当年 entries
    // のみ別途 fetch する。前年 BCB が未確定/欠落の場合は priorCumulative=
    // 空のまま当年 entries だけで描画する (degraded fallback)。
    let priorCumulative = {};
    if (params.year > params.min_year) {
      try {
        const blobs = await fetchBalanceCacheBlobs({
          client, userId: params.user_id, fiscalYear: params.year - 1,
        });
        if (blobs[15]) priorCumulative = blobs[15];
      } catch (e) {
        console.warn(
          "balance_sheet_renderer: prior BCB fetch failed, priorCumulative={}",
          e,
        );
      }
    }
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });

    const jsResult = computeBalanceSheet(entries, {
      accountTypeByCode, normalBalanceByCode, accountNameByCode,
      priorCumulative,
    });
    _renderView(composeBalanceSheetView(jsResult));
  } catch (e) {
    _setStatus("貸借対照表の取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
