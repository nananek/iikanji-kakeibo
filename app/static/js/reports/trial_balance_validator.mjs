// Trial balance 画面のサーバ集計値 (HTML 描画値) と
// クライアント集計値 (computeTrialBalance) を並列実行して比較する fail-soft プローブ。
// 表示には影響しない (defer 読込)。MK 未ロード時は skip。


export function compareTrialBalance(serverRows, jsRows) {
  // serverRows: [{code, debit, credit}]
  // jsRows: [{account_code, debit, credit}]
  const byJs = new Map(jsRows.map((r) => [r.account_code, r]));
  const diffs = [];
  for (const sv of serverRows) {
    const js = byJs.get(sv.code);
    if (!js) {
      // Server may emit B/S accounts whose period activity is zero
      // (opening balance only). Client-side aggregation skips such
      // accounts, so suppress missing_in_client for zero rows to avoid
      // false positives.
      if (sv.debit !== 0 || sv.credit !== 0) {
        diffs.push({ code: sv.code, kind: "missing_in_client" });
      }
      continue;
    }
    if (js.debit !== sv.debit || js.credit !== sv.credit) {
      diffs.push({
        code: sv.code, kind: "mismatch",
        server: { debit: sv.debit, credit: sv.credit },
        client: { debit: js.debit, credit: js.credit },
      });
    }
  }
  const serverCodes = new Set(serverRows.map((r) => r.code));
  for (const js of jsRows) {
    if (!serverCodes.has(js.account_code)
        && (js.debit !== 0 || js.credit !== 0)) {
      diffs.push({
        code: js.account_code, kind: "extra_in_client",
        client: { debit: js.debit, credit: js.credit },
      });
    }
  }
  return diffs;
}


// SharedWorker は URL でインスタンスを共有するため、他の crypto UI
// (ウィザード / AI 設定) と完全に同じ URL を使う必要がある。違う URL を
// 渡すと別 Worker が起きて MK がロードされておらず常に skip になる。
function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


async function _run() {
  const paramsEl = document.getElementById("trial-balance-server-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("trial_balance_validator: failed to parse server params", e);
    return;
  }
  // GET /api/v1/journals authenticates as current_user (Flask-Login),
  // not the audited owner. Skipping in audit-proxy mode avoids fetching
  // the auditor's own ledger and comparing it against the owner's
  // server-rendered totals, which would always look like a mismatch.
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;

  const serverRows = [];
  document.querySelectorAll("[data-trial-balance-row]").forEach((tr) => {
    serverRows.push({
      code: tr.getAttribute("data-trial-balance-row"),
      debit: parseInt(tr.getAttribute("data-server-debit") || "0", 10),
      credit: parseInt(tr.getAttribute("data-server-credit") || "0", 10),
    });
  });
  if (serverRows.length === 0) return;

  // 動的 import で画面表示のクリティカルパスから外す
  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeTrialBalance }]
    = await Promise.all([
      import("/static/js/crypto/shared-client.js"),
      import("/static/js/crypto/journals_client.js"),
      import("/static/js/crypto/reports/trial_balance.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("trial_balance_validator: MK locked, skipping validation");
      return;
    }
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.fiscal_year,
    });
    const jsRows = computeTrialBalance(entries, {
      fiscalPeriodFrom: params.fiscal_period_from,
      fiscalPeriodTo: params.fiscal_period_to,
    });
    const diffs = compareTrialBalance(serverRows, jsRows);
    if (diffs.length === 0) {
      console.info(
        `%c✓ trial_balance: server vs client ${serverRows.length} 行一致`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ trial_balance: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("trial_balance_validator: error", e);
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
