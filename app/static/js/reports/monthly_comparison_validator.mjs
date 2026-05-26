// 月次比較画面のサーバ集計と クライアント集計 (computeMonthlyComparison) を
// 並列実行して比較する fail-soft プローブ。表示には影響しない。
// MK 未ロード時 / 監査代理閲覧時は skip。
//
// 事業科目 (biz_codes) はサーバ側で _collapse_business_accounts によって
// comparison から除外済み。validator は「サーバが data-monthly-row 行として
// 描画した科目」だけを比較対象とする (P/L validator と同じ戦略)。


function arraysEqual(a, b) {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}


export function compareMonthlyComparison(serverRows, jsResult) {
  // serverRows: [{code, type: "income"|"expense", months: [12], total}]
  // jsResult: {
  //   income_accounts: [{code, months: [12], total}],
  //   expense_accounts: [{code, months: [12], total}]
  // }
  const byJs = new Map();
  for (const a of jsResult.income_accounts) {
    byJs.set(a.code, { type: "income", months: a.months, total: a.total });
  }
  for (const a of jsResult.expense_accounts) {
    byJs.set(a.code, { type: "expense", months: a.months, total: a.total });
  }

  const diffs = [];
  for (const sv of serverRows) {
    const js = byJs.get(sv.code);
    if (!js) {
      const hasNonZero = sv.total !== 0 || sv.months.some((v) => v !== 0);
      if (hasNonZero) {
        diffs.push({ code: sv.code, kind: "missing_in_client" });
      }
      continue;
    }
    if (js.type !== sv.type
        || js.total !== sv.total
        || !arraysEqual(js.months, sv.months)) {
      diffs.push({
        code: sv.code, kind: "mismatch",
        server: { type: sv.type, total: sv.total, months: sv.months },
        client: { type: js.type, total: js.total, months: js.months },
      });
    }
  }
  const serverCodes = new Set(serverRows.map((r) => r.code));
  for (const [code, info] of byJs) {
    if (!serverCodes.has(code)) {
      const hasNonZero = info.total !== 0 || info.months.some((v) => v !== 0);
      if (hasNonZero) {
        diffs.push({ code, kind: "extra_in_client", client: info });
      }
    }
  }
  return diffs;
}


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


async function _run() {
  const paramsEl = document.getElementById("monthly-server-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("monthly_comparison_validator: failed to parse server params", e);
    return;
  }
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;

  const serverRows = [];
  const accountTypeByCode = {};
  document.querySelectorAll("[data-monthly-row]").forEach((tr) => {
    const code = tr.getAttribute("data-monthly-row");
    const type = tr.getAttribute("data-monthly-row-type");
    let months;
    try {
      months = JSON.parse(tr.getAttribute("data-server-months") || "[]");
    } catch (_e) {
      months = [];
    }
    if (!Array.isArray(months) || months.length !== 12) return;
    const total = parseInt(tr.getAttribute("data-server-total") || "0", 10);
    // amount は浮動小数の可能性があるので int 化はサーバ側 (Number→int)
    // に委ね、ここでは Number 化のみ
    const monthsInt = months.map((v) => Number(v) | 0);
    serverRows.push({ code, type, months: monthsInt, total });
    accountTypeByCode[code] = (type === "income") ? "revenue" : "expense";
  });
  if (serverRows.length === 0) return;

  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeMonthlyComparison }]
    = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/monthly_comparison.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("monthly_comparison_validator: MK locked, skipping");
      return;
    }
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeMonthlyComparison(entries, { accountTypeByCode });

    const diffs = compareMonthlyComparison(serverRows, {
      income_accounts: jsResult.income_accounts.map(
        (a) => ({ code: a.code, months: a.months, total: a.total }),
      ),
      expense_accounts: jsResult.expense_accounts.map(
        (a) => ({ code: a.code, months: a.months, total: a.total }),
      ),
    });
    if (diffs.length === 0) {
      console.info(
        `%c✓ monthly_comparison: server vs client ${serverRows.length} 行一致`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ monthly_comparison: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("monthly_comparison_validator: error", e);
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
