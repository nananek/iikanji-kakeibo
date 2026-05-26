// 損益計算書 (P/L) のクライアント描画。
// MK 復号→集計→DOM 構築まで完結する (browser-only)。
//
// 監査代理閲覧時は API が effective user (= owner) を解決するが、
// 監査者の MK でオーナーの暗号化 entries は復号できないため、空表示になる。


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
  const el = document.getElementById("pl-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("pl-status");
  if (el) el.classList.add("d-none");
}


function _setSummary(view) {
  const incomeEl = document.getElementById("pl-summary-income");
  const expenseEl = document.getElementById("pl-summary-expense");
  const balanceEl = document.getElementById("pl-summary-balance");
  const balanceCard = document.getElementById("pl-summary-balance-card");
  if (incomeEl) incomeEl.textContent = _fmtYen(view.summary.income);
  if (expenseEl) expenseEl.textContent = _fmtYen(view.summary.expense);
  if (balanceEl) balanceEl.textContent = _fmtYen(view.summary.balance);
  if (balanceCard) {
    balanceCard.classList.remove("text-bg-success", "text-bg-warning");
    balanceCard.classList.add(
      view.summary.balance >= 0 ? "text-bg-success" : "text-bg-warning",
    );
  }
}


function _renderBreakdown(tbodyId, rows, emptyMsgId, leadingRow) {
  const tbody = document.getElementById(tbodyId);
  const emptyMsg = document.getElementById(emptyMsgId);
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

  const hasData = (leadingRow !== null) || rows.length > 0;
  if (emptyMsg) emptyMsg.classList.toggle("d-none", hasData);
  if (tbody.parentElement) {
    tbody.parentElement.classList.toggle("d-none", !hasData);
  }

  if (leadingRow) {
    // 事業所得行 (bizIncome) を income テーブル先頭に挿入
    const tr = document.createElement("tr");
    tr.className = "table-success";
    const labelTd = document.createElement("td");
    const strong = document.createElement("strong");
    strong.textContent = leadingRow.label;
    labelTd.appendChild(document.createTextNode("📁 "));
    labelTd.appendChild(strong);
    if (leadingRow.href) {
      const a = document.createElement("a");
      a.href = leadingRow.href;
      a.className = "ms-1 small text-decoration-none";
      a.textContent = "内訳";
      labelTd.appendChild(document.createTextNode(" "));
      labelTd.appendChild(a);
    }
    const amountTd = document.createElement("td");
    amountTd.className = "text-end";
    const amountStrong = document.createElement("strong");
    amountStrong.textContent = _fmtYen(leadingRow.amount);
    amountTd.appendChild(amountStrong);
    tr.appendChild(labelTd);
    tr.appendChild(amountTd);
    tbody.appendChild(tr);
  }

  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.setAttribute("data-pl-row", row.account_code);
    const labelTd = document.createElement("td");
    labelTd.textContent = row.account_name;
    const amountTd = document.createElement("td");
    amountTd.className = "text-end";
    amountTd.textContent = _fmtYen(row.amount);
    tr.appendChild(labelTd);
    tr.appendChild(amountTd);
    tbody.appendChild(tr);
  }
}


function _renderView(view, params) {
  _setSummary(view);

  const leading = view.bizIncome ? {
    label: "事業所得",
    href: params.tax_form_url || null,
    amount: view.bizIncome.income,
  } : null;

  _renderBreakdown(
    "pl-income-tbody", view.income_breakdown, "pl-income-empty", leading,
  );
  _renderBreakdown(
    "pl-expense-tbody", view.expense_breakdown, "pl-expense-empty", null,
  );
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("profit_loss_renderer: " + msg, logArg);
  _setStatus("損益計算書の初期化に失敗しました: " + msg, "danger");
}


async function _run() {
  const paramsEl = document.getElementById("pl-server-params");
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

  const accountsEl = document.getElementById("pl-accounts-meta");
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
      { computeProfitLoss },
      { composeProfitLossView },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/profit_loss.js"),
      import(getStaticRoot() + "js/crypto/reports/profit_loss_view.js"),
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
        "監査代理閲覧中です。オーナーの暗号化された仕訳はあなたの暗号鍵では復号できないため、損益計算書は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    // 事業科目は P/L から除外。accountTypeByCode の生成時に biz_codes を skip
    const accountTypeByCode = {};
    const accountNameByCode = {};
    for (const [code, meta] of Object.entries(accountsMeta)) {
      if (meta.is_business) continue;
      accountTypeByCode[code] = meta.type;
      accountNameByCode[code] = meta.name;
    }

    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeProfitLoss(entries, {
      accountTypeByCode,
      accountNameByCode,
      month: params.month || undefined,
    });
    const view = composeProfitLossView(jsResult, {
      bizIncome: params.biz_income || null,
    });
    _renderView(view, params);
  } catch (e) {
    _setStatus("損益計算書の取得に失敗しました: " + (e.message || e), "danger");
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
