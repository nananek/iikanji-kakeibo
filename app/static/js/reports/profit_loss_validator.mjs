// P/L 画面のサーバ集計値 (HTML 描画値) と
// クライアント集計値 (computeProfitLoss) を並列実行して比較する fail-soft プローブ。
// 表示には影響しない。MK 未ロード時 / 監査代理閲覧時は skip。
//
// 事業科目 (biz_codes) はサーバ側で P/L から除外済み。validator は
// 「サーバが表示した行 (data-pl-row=*)」だけを比較対象とすることで、
// 事業科目を validator 側で再現する必要をなくしている。


export function compareProfitLoss(serverRows, jsResult) {
  // serverRows: [{code, type: "income"|"expense", amount}]
  // jsResult: { income_breakdown: [{account_code, amount}],
  //             expense_breakdown: [{account_code, amount}] }
  const byJs = new Map();
  for (const r of jsResult.income_breakdown) {
    byJs.set(r.account_code, { type: "income", amount: r.amount });
  }
  for (const r of jsResult.expense_breakdown) {
    byJs.set(r.account_code, { type: "expense", amount: r.amount });
  }

  const diffs = [];
  for (const sv of serverRows) {
    const js = byJs.get(sv.code);
    if (!js) {
      // ゼロ行は computeProfitLoss が breakdown に出さない仕様のため
      // missing_in_client から除外する (試算表 validator と同じ非対称回避)
      if (sv.amount !== 0) {
        diffs.push({ code: sv.code, kind: "missing_in_client" });
      }
      continue;
    }
    if (js.amount !== sv.amount || js.type !== sv.type) {
      diffs.push({
        code: sv.code, kind: "mismatch",
        server: { type: sv.type, amount: sv.amount },
        client: js,
      });
    }
  }
  const serverCodes = new Set(serverRows.map((r) => r.code));
  for (const [code, info] of byJs) {
    if (!serverCodes.has(code) && info.amount !== 0) {
      diffs.push({ code, kind: "extra_in_client", client: info });
    }
  }
  return diffs;
}


// SharedWorker は URL でインスタンス共有されるため、validator が起動する
// SharedCryptoClient を他の crypto UI と同じ Worker に届かせる必要がある。
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
  const paramsEl = document.getElementById("pl-server-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("profit_loss_validator: failed to parse server params", e);
    return;
  }
  // GET /api/v1/journals authenticates as current_user via Flask-Login,
  // not acting_as_user_id — skip under audit proxy to avoid comparing
  // the owner's totals with the auditor's own ledger.
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;

  const serverRows = [];
  const accountTypeByCode = {};
  document.querySelectorAll("[data-pl-row]").forEach((tr) => {
    const code = tr.getAttribute("data-pl-row");
    const type = tr.getAttribute("data-pl-row-type");
    const amount = parseInt(tr.getAttribute("data-server-amount") || "0", 10);
    serverRows.push({ code, type, amount });
    // computeProfitLoss はサーバ表示行に限定して集計させたいので、
    // 「サーバが出した code」だけ revenue/expense にマップする。
    accountTypeByCode[code] = (type === "income") ? "revenue" : "expense";
  });
  if (serverRows.length === 0) return;

  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeProfitLoss }]
    = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/profit_loss.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("profit_loss_validator: MK locked, skipping validation");
      return;
    }
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeProfitLoss(entries, {
      accountTypeByCode,
      month: params.month || undefined,
    });
    // computeProfitLoss が income に分類した行を { account_code, amount } で
    // 受け取り、compareProfitLoss と同じインタフェースに揃える
    const diffs = compareProfitLoss(serverRows, {
      income_breakdown: jsResult.income_breakdown.map(
        (r) => ({ account_code: r.account_code, amount: r.amount }),
      ),
      expense_breakdown: jsResult.expense_breakdown.map(
        (r) => ({ account_code: r.account_code, amount: r.amount }),
      ),
    });
    if (diffs.length === 0) {
      console.info(
        `%c✓ profit_loss: server vs client ${serverRows.length} 行一致`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ profit_loss: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("profit_loss_validator: error", e);
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
