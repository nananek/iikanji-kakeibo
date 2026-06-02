// 試算表 (残高試算表) のクライアント描画。
// MK 復号→集計→DOM 構築まで完結する (browser-only)。
//
// 監査代理閲覧時は API が effective user (= owner) を解決するが、
// 監査者の MK でオーナーの暗号化 entries は復号できないため、空表示になる。
// (Lv1 は API 自体が 403。Lv2/Lv3 は復号失敗 → 空)


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
  return "¥" + n.toLocaleString();
}


function _setStatusMessage(msg, type = "info") {
  const el = document.getElementById("trial-balance-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("trial-balance-status");
  if (el) el.classList.add("d-none");
}


function _clearTbody() {
  const tbody = document.getElementById("trial-balance-tbody");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
}


function _td(text, opts = {}) {
  const td = document.createElement("td");
  if (opts.className) td.className = opts.className;
  if (opts.colSpan) td.colSpan = opts.colSpan;
  if (opts.bold) {
    const strong = document.createElement("strong");
    strong.textContent = text;
    td.appendChild(strong);
  } else {
    td.textContent = text;
  }
  return td;
}


function _ledgerHref(code, params) {
  const u = new URL("/reports/ledger", globalThis.location.origin);
  u.searchParams.set("year", String(params.fiscal_year));
  u.searchParams.set("account_code", code);
  u.searchParams.set("pf", String(params.fiscal_period_from));
  u.searchParams.set("pt", String(params.fiscal_period_to));
  return u.pathname + u.search;
}


function _renderSection(section, params) {
  const frag = document.createDocumentFragment();
  // 区分ヘッダー
  const header = document.createElement("tr");
  header.className = "table-secondary";
  header.appendChild(_td(section.typeName, { colSpan: 5, bold: true }));
  frag.appendChild(header);

  for (const row of section.rows) {
    const tr = document.createElement("tr");
    tr.setAttribute("data-trial-balance-row", row.code);
    tr.appendChild(_td(row.code, { className: "d-mobile-none" }));
    // 科目名は元帳へのリンクにする (textContent でユーザー入力を扱う)
    const nameTd = document.createElement("td");
    const a = document.createElement("a");
    a.className = "text-decoration-none";
    a.href = _ledgerHref(row.code, params);
    a.textContent = row.name;
    nameTd.appendChild(a);
    tr.appendChild(nameTd);
    tr.appendChild(_td(row.debit ? _fmtYen(row.debit) : "", { className: "text-end" }));
    tr.appendChild(_td(row.credit ? _fmtYen(row.credit) : "", { className: "text-end" }));
    tr.appendChild(_td(_fmtYen(row.balance), {
      className: "text-end" + (row.balance < 0 ? " text-danger" : ""),
    }));
    frag.appendChild(tr);
  }

  // 小計
  const sub = document.createElement("tr");
  sub.className = "table-info";
  sub.appendChild(_td("", { className: "d-mobile-none" }));
  sub.appendChild(_td(section.typeName + " 小計", { className: "text-end", bold: true }));
  sub.appendChild(_td(_fmtYen(section.subtotal.debit), { className: "text-end", bold: true }));
  sub.appendChild(_td(_fmtYen(section.subtotal.credit), { className: "text-end", bold: true }));
  sub.appendChild(_td(_fmtYen(section.subtotal.balance), { className: "text-end", bold: true }));
  frag.appendChild(sub);
  return frag;
}


function _renderGrandTotalRow(view) {
  // 合計試算表: 期中の借方/貸方総合計 (仕訳整合性から本質的に一致)
  const tr = document.createElement("tr");
  tr.className = "table-dark";
  tr.appendChild(_td("", { className: "d-mobile-none" }));
  tr.appendChild(_td("総合計 (期中取引)", {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(_fmtYen(view.grandTotal.debit), {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(_fmtYen(view.grandTotal.credit), {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(""));
  return tr;
}


function _renderBalanceCheckRow(view) {
  // 残高試算表: 借方科目 (資産+費用) 残高合計 === 貸方科目 (負債+純資産+収益)
  // 残高合計 を確認する行。前期繰越誤り・科目区分誤り等を検知できる。
  const gt = view.grandTotal;
  const tr = document.createElement("tr");
  tr.className = gt.is_balanced ? "table-success" : "table-warning";
  tr.appendChild(_td("", { className: "d-mobile-none" }));
  tr.appendChild(_td("残高チェック (借方科目 = 貸方科目)", {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(_fmtYen(gt.balance_debit_side), {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(_fmtYen(gt.balance_credit_side), {
    className: "text-end", bold: true,
  }));
  const checkTd = document.createElement("td");
  checkTd.className = "text-end";
  const icon = document.createElement("i");
  icon.className = gt.is_balanced
    ? "bi bi-check-circle text-success"
    : "bi bi-exclamation-triangle text-danger";
  checkTd.appendChild(icon);
  if (!gt.is_balanced) {
    checkTd.appendChild(document.createTextNode(" 差額 "));
    const diffStrong = document.createElement("strong");
    diffStrong.textContent = _fmtYen(gt.balance_debit_side - gt.balance_credit_side);
    checkTd.appendChild(diffStrong);
  }
  tr.appendChild(checkTd);
  return tr;
}


function _renderView(view, params) {
  const tbody = document.getElementById("trial-balance-tbody");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  if (view.sections.length === 0) {
    const tr = document.createElement("tr");
    tr.appendChild(_td("表示できる仕訳がありません", {
      colSpan: 5, className: "text-center text-muted py-4",
    }));
    tbody.appendChild(tr);
    return;
  }
  for (const section of view.sections) {
    tbody.appendChild(_renderSection(section, params));
  }
  tbody.appendChild(_renderGrandTotalRow(view));
  tbody.appendChild(_renderBalanceCheckRow(view));
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("trial_balance_renderer: " + msg, logArg);
  _setStatusMessage("試算表ページの初期化に失敗しました: " + msg, "danger");
  _clearTbody();
}


async function _run() {
  const paramsEl = document.getElementById("trial-balance-server-params");
  if (!paramsEl) { _abort("server params script not found"); return; }
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    _abort("failed to parse params", e);
    return;
  }
  if (typeof params.user_id !== "number"
      || typeof params.fiscal_year !== "number") {
    _abort("invalid params (user_id/fiscal_year)");
    return;
  }

  const accountsEl = document.getElementById("trial-balance-accounts-meta");
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
      { computeTrialBalance },
      { composeTrialBalanceView },
      { fetchBalanceCacheBlobs },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/trial_balance.js"),
      import(getStaticRoot() + "js/crypto/reports/trial_balance_view.js"),
      import(getStaticRoot() + "js/crypto/balance_cache_blobs_client.js"),
    ]);

    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      _setStatusMessage(
        "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
        "warning",
      );
      _clearTbody();
      return;
    }
    _clearStatus();
    const pf = params.fiscal_period_from;
    const pt = params.fiscal_period_to;
    // #230: BCB fetch と journals fetch を並列化。pf=0 (期首から) のときは
    // BCB 不要なので空 dict を即 resolve、pf>=1 で前 period の blob を取る。
    const bcbPromise = pf >= 1
      ? fetchBalanceCacheBlobs({
          client, userId: params.user_id, fiscalYear: params.fiscal_year,
        }).catch((e) => {
          console.warn("trial_balance_renderer: BCB fetch failed, opening=0", e);
          return {};
        })
      : Promise.resolve({});
    const journalsPromise = fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.fiscal_year,
    });
    const [blobs, entries] = await Promise.all([bcbPromise, journalsPromise]);

    const opening = {};
    if (pf >= 1) {
      const cacheForPeriod = blobs[pf - 1];
      if (cacheForPeriod) {
        for (const [code, pair] of Object.entries(cacheForPeriod)) {
          const meta = accountsMeta[code];
          if (!meta || !Array.isArray(pair) || pair.length < 2) continue;
          const [cumD, cumC] = pair;
          opening[code] = meta.normal_balance === "debit"
            ? (cumD - cumC) : (cumC - cumD);
        }
      }
    }
    const jsRows = computeTrialBalance(entries, {
      fiscalPeriodFrom: pf,
      fiscalPeriodTo: pt,
      // 振替期間 (period 16) を含む場合は損益振替仕訳も集計対象とする
      includeClosing: pt >= 16,
    });
    const view = composeTrialBalanceView(jsRows, accountsMeta, { opening });
    if (view.unmappedCodes && view.unmappedCodes.length > 0) {
      // #221 安全網: accountsMeta で解決できなかった科目コードを記録する。
      // 暗号化スキーマ変更 / 科目削除 / Lv2 隠蔽の組合せ等で発生し得る。
      console.warn(
        "trial_balance_renderer: 未マップ科目コードがありました (試算表から除外)",
        view.unmappedCodes,
      );
    }
    _renderView(view, params);
  } catch (e) {
    _setStatusMessage("試算表の取得に失敗しました: " + (e.message || e), "danger");
    _clearTbody();
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
