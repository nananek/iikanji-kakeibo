// Phase E3-C-2b: 試算表 (残高試算表) のサーバ集計 vs クライアント集計の
// 並列検証スクリプト。
//
// 起動時に:
//   1. ページ内の `#trial-balance-accounts-data` / `#trial-balance-server-params`
//      JSON と <tr data-trial-balance-row="..."> の data-server-* 属性を読む
//   2. journals_client.fetchJournalsForYear で当年度の暗号化済仕訳を取得
//   3. computeTrialBalance で集計
//   4. 各 account_code ごとに server debit/credit と JS 結果を比較
//   5. console.log で結果出力 (一致 = ✓、不一致 = ⚠)
//
// 本スクリプトは表示には影響しない。Phase E7 一斉移行前に開発者・運用者が
// 動作確証を得るための検証用途。MK がロックされている場合は skip する。


export function compareTrialBalance(serverRows, jsRows) {
  // serverRows: [{code, debit, credit}]
  // jsRows: [{account_code, debit, credit}]
  const byJs = new Map(jsRows.map((r) => [r.account_code, r]));
  const diffs = [];
  for (const sv of serverRows) {
    const js = byJs.get(sv.code);
    if (!js) {
      diffs.push({ code: sv.code, kind: "missing_in_client" });
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
  // クライアントのみに存在する科目
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


async function _run() {
  const paramsEl = document.getElementById("trial-balance-server-params");
  const accountsEl = document.getElementById("trial-balance-accounts-data");
  if (!paramsEl || !accountsEl) return;  // 検証対象ページではない

  let params, accounts;
  try {
    params = JSON.parse(paramsEl.textContent);
    accounts = JSON.parse(accountsEl.textContent);
  } catch (e) {
    console.warn("trial_balance_validator: failed to parse JSON data", e);
    return;
  }

  // サーバが描画した <tr> 各行から data-server-{debit,credit} を回収
  const serverRows = [];
  document.querySelectorAll("[data-trial-balance-row]").forEach((tr) => {
    serverRows.push({
      code: tr.getAttribute("data-trial-balance-row"),
      debit: parseInt(tr.getAttribute("data-server-debit") || "0", 10),
      credit: parseInt(tr.getAttribute("data-server-credit") || "0", 10),
    });
  });
  if (serverRows.length === 0) return;

  // 動的 import: 検証スクリプトは画面表示のクリティカルパスに乗らない
  // (defer 読込)。MK 未ロード時は skip。
  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeTrialBalance }]
    = await Promise.all([
      import("/static/js/crypto/shared-client.js"),
      import("/static/js/crypto/journals_client.js"),
      import("/static/js/crypto/reports/trial_balance.js"),
    ]);

  const client = new SharedCryptoClient("/static/js/crypto/shared-worker.js");
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("trial_balance_validator: MK locked, skipping validation");
      return;
    }
    // userId: 一旦 Web セッション側で取得する手段がないので、
    // wrapped_keys API から自分のユーザー id を逆引きするのも面倒なので
    // window.IIKANJI_USER_ID 等を base.html で埋め込む将来拡張に委ねる。
    // ここでは存在チェックのみ。
    const userId = globalThis.IIKANJI_USER_ID;
    if (typeof userId !== "number") {
      console.info(
        "trial_balance_validator: IIKANJI_USER_ID not exposed yet, " +
        "skipping (validator は将来 base.html での user_id 埋め込みに依存)",
      );
      return;
    }

    const entries = await fetchJournalsForYear({
      client, userId, fiscalYear: params.fiscal_year,
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
        `%c⚠ trial_balance: ${diffs.length} 件の不一致 (Phase E7 切替前に要調査)`,
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


// DOMContentLoaded 後に自動実行
if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
