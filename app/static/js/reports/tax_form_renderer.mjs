// 青色申告決算書 (tax form) のクライアント描画。
// MK 復号 → 前年 BCB period=15 (bs_opening) + 当年 entries を fetch →
// P/L/B/S 期末/期首の code→amount マップを構築 → composeTaxFormView →
// DOM 構築。
//
// #221 BCB 統合: 旧来は min_year..year を順次 fetch していたが、前年末
// 累計を BCB 1 リクエストで取得して当年 entries に加算する形に変更。


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
  const el = document.getElementById("tax-form-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("tax-form-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("tax_form_renderer: " + msg, logArg);
  _setStatus("青色申告決算書の初期化に失敗しました: " + msg, "danger");
}


/**
 * entries 配列から code → net amount (normal_balance 側を正符号) の
 * マップを作る。
 *
 * @param {Array<Object>} entries
 * @param {Object} accountsMeta {[code]: {normal_balance}}
 * @param {Object} options
 * @param {number|null} [options.fiscalYearOnly] 当該 fiscal_year のみ
 * @param {boolean} [options.includeClosing=false]
 */
function _collectAmounts(entries, accountsMeta, options = {}) {
  const fiscalYearOnly = options.fiscalYearOnly ?? null;
  const includeClosing = options.includeClosing ?? false;
  // code → {debit, credit}
  const sums = new Map();
  for (const entry of entries) {
    if (!includeClosing && entry.source === "closing") continue;
    if (fiscalYearOnly != null && entry.fiscal_year !== fiscalYearOnly) {
      continue;
    }
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (!code) continue;
      const cur = sums.get(code) ?? { debit: 0, credit: 0 };
      cur.debit += line.debit ?? 0;
      cur.credit += line.credit ?? 0;
      sums.set(code, cur);
    }
  }
  const result = {};
  for (const [code, { debit, credit }] of sums.entries()) {
    const meta = accountsMeta[code];
    if (!meta) continue;
    if (meta.normal_balance === "debit") {
      result[code] = debit - credit;
    } else {
      result[code] = credit - debit;
    }
  }
  return result;
}


/**
 * BCB の {accountCode: [debit_cum, credit_cum]} を、normal_balance 側で
 * netted した {accountCode: amount} に変換する。
 *
 * 期首残高 (bs_opening) を BCB 累計から作るときに使用。BS 科目
 * (asset/liability/equity) のみを返し、P/L 科目 (revenue/expense) は
 * 除外する (旧サーバ実装の B/S 集計と挙動を一致させるため。BCB には
 * 全科目の累計が含まれる)。
 *
 * export している理由: テスト容易性 (test_tax_form_net_cumulative.mjs
 * から呼び出す。renderer 全体のモックは複雑なため)。
 */
const _BS_TYPES = new Set(["asset", "liability", "equity"]);

export function _netCumulative(cumulative, accountsMeta) {
  const result = {};
  for (const [code, pair] of Object.entries(cumulative || {})) {
    if (!Array.isArray(pair) || pair.length < 2) continue;
    const meta = accountsMeta[code];
    if (!meta) continue;
    if (meta.type && !_BS_TYPES.has(meta.type)) continue;
    const d = pair[0] || 0;
    const c = pair[1] || 0;
    result[code] = meta.normal_balance === "debit" ? d - c : c - d;
  }
  return result;
}


const SECTION_LABELS = {
  revenue: "売上（収入）",
  cost_of_sales: "売上原価",
  expenses: "経費",
  income: "所得金額",
  bs_assets: "資産の部",
  bs_liabilities: "負債・資本の部",
};


function _renderPLBody(field_data) {
  const tbody = document.getElementById("tax-form-pl-tbody");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  let currentSection = null;
  for (const item of field_data) {
    const f = item.field;
    if (f.page !== 1) continue;
    if (f.section !== currentSection) {
      currentSection = f.section;
      const tr = document.createElement("tr");
      tr.className = "table-secondary";
      const td = document.createElement("td");
      td.colSpan = 3;
      const strong = document.createElement("strong");
      strong.textContent = SECTION_LABELS[f.section] || f.section;
      td.appendChild(strong);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    tbody.appendChild(_renderPLRow(item));
  }
}


function _renderPLRow(item) {
  const f = item.field;
  const tr = document.createElement("tr");
  if (f.is_subtotal) {
    tr.className = "table-info fw-bold";
  } else if (f.is_user_defined && (!item.codes || item.codes.length === 0)) {
    tr.className = "text-muted";
  }

  const codeTd = document.createElement("td");
  codeTd.className = "text-muted";
  const codeSmall = document.createElement("small");
  codeSmall.textContent = f.row_code;
  codeTd.appendChild(codeSmall);
  tr.appendChild(codeTd);

  const nameTd = document.createElement("td");
  nameTd.appendChild(document.createTextNode(f.name));
  if (!f.is_subtotal && (!item.codes || item.codes.length === 0)) {
    nameTd.appendChild(document.createTextNode(" "));
    const small = document.createElement("small");
    small.className = "text-muted";
    small.textContent = "(未設定)";
    nameTd.appendChild(small);
  }
  tr.appendChild(nameTd);

  const amountTd = document.createElement("td");
  amountTd.className = "text-end";
  if (f.is_subtotal) {
    amountTd.textContent = _fmtYen(item.amount);
  } else if (item.amount) {
    amountTd.textContent = _fmtYen(item.amount);
  } else {
    amountTd.textContent = "-";
  }
  tr.appendChild(amountTd);
  return tr;
}


function _renderBSBody(field_data) {
  const tbody = document.getElementById("tax-form-bs-tbody");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  let currentSection = null;
  for (const item of field_data) {
    const f = item.field;
    if (f.page !== 4) continue;
    if (f.section !== currentSection) {
      currentSection = f.section;
      const tr = document.createElement("tr");
      tr.className = "table-secondary";
      const td = document.createElement("td");
      td.colSpan = 4;
      const strong = document.createElement("strong");
      strong.textContent = SECTION_LABELS[f.section] || f.section;
      td.appendChild(strong);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    tbody.appendChild(_renderBSRow(item));
  }
}


function _renderBSRow(item) {
  const f = item.field;
  const tr = document.createElement("tr");
  if (f.is_subtotal) {
    tr.className = "table-info fw-bold";
  } else if (f.is_user_defined && (!item.codes || item.codes.length === 0)) {
    tr.className = "text-muted";
  }

  const codeTd = document.createElement("td");
  codeTd.className = "text-muted";
  const codeSmall = document.createElement("small");
  codeSmall.textContent = f.row_code;
  codeTd.appendChild(codeSmall);
  tr.appendChild(codeTd);

  const nameTd = document.createElement("td");
  nameTd.appendChild(document.createTextNode(f.name));
  if (!f.is_subtotal && (!item.codes || item.codes.length === 0)) {
    nameTd.appendChild(document.createTextNode(" "));
    const small = document.createElement("small");
    small.className = "text-muted";
    small.textContent = "(未設定)";
    nameTd.appendChild(small);
  }
  tr.appendChild(nameTd);

  const openingTd = document.createElement("td");
  openingTd.className = "text-end";
  if (f.is_subtotal) {
    openingTd.textContent = _fmtYen(item.opening || 0);
  } else if (item.opening) {
    openingTd.textContent = _fmtYen(item.opening);
  } else {
    openingTd.textContent = "-";
  }
  tr.appendChild(openingTd);

  const amountTd = document.createElement("td");
  amountTd.className = "text-end";
  if (f.is_subtotal) {
    amountTd.textContent = _fmtYen(item.amount);
  } else if (item.amount) {
    amountTd.textContent = _fmtYen(item.amount);
  } else {
    amountTd.textContent = "-";
  }
  tr.appendChild(amountTd);
  return tr;
}


function _renderView(view) {
  _renderPLBody(view.field_data);
  _renderBSBody(view.field_data);
}


async function _run() {
  const paramsEl = document.getElementById("tax-form-server-params");
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

  const accountsEl = document.getElementById("tax-form-accounts-meta");
  const structureEl = document.getElementById("tax-form-structure");
  let accountsMeta = {};
  let structure = { fields: [], mappings: {} };
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    _abort("failed to parse accounts meta", e);
    return;
  }
  try {
    if (structureEl) structure = JSON.parse(structureEl.textContent);
  } catch (e) {
    _abort("failed to parse form structure", e);
    return;
  }

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { composeTaxFormView },
      { fetchBalanceCacheBlobs },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/tax_form_view.js"),
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
        "監査代理閲覧中です。オーナーの暗号化された仕訳はあなたの暗号鍵では復号できないため、決算書は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    // 仕訳ゼロ件 (min_year=null) なら空 view を描画
    if (params.min_year == null) {
      const view = composeTaxFormView(
        { pl_amounts: {}, bs_amounts: {}, bs_opening: {} },
        structure,
      );
      _renderView(view);
      return;
    }

    // BCB 統合 (#221): 前年 BCB period=15 + 当年 entries の 2 リクエストに
    // 削減。bs_opening は前年末累計を netted した値、bs_amounts は
    // bs_opening + 当年 BS 累計 (closing 含む) で計算する。
    // #230: 前年 BCB fetch と当年 journals fetch を並列化。
    const bcbPromise = params.year > params.min_year
      ? fetchBalanceCacheBlobs({
          client, userId: params.user_id, fiscalYear: params.year - 1,
        }).catch((e) => {
          console.warn(
            "tax_form_renderer: prior BCB fetch failed (前年が月次確定済かを確認してください), priorCumulative={}",
            e,
          );
          return {};
        })
      : Promise.resolve({});
    const journalsPromise = fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const [blobs, entries] = await Promise.all([bcbPromise, journalsPromise]);
    const priorCumulative = blobs[15] || {};

    // pl_amounts は当年 fiscal_year のみ + closing 除外
    const pl_amounts = _collectAmounts(entries, accountsMeta, {
      fiscalYearOnly: params.year, includeClosing: false,
    });
    // bs_opening = 前年末累計を normal_balance 側で netted
    const bs_opening = _netCumulative(priorCumulative, accountsMeta);
    // 当年の BS 系科目累計 (closing 含む)
    const currentBs = _collectAmounts(entries, accountsMeta, {
      includeClosing: true,
    });
    // bs_amounts = bs_opening + 当年 BS 累計
    const bs_amounts = { ...bs_opening };
    for (const [code, amt] of Object.entries(currentBs)) {
      bs_amounts[code] = (bs_amounts[code] || 0) + amt;
    }

    const view = composeTaxFormView(
      { pl_amounts, bs_amounts, bs_opening },
      structure,
    );
    _renderView(view);
  } catch (e) {
    _setStatus("青色申告決算書の取得に失敗しました: " + (e.message || e), "danger");
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
