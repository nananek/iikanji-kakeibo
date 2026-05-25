// 元帳画面のサーバ集計とクライアント集計 (computeLedger) を並列実行して
// 比較する fail-soft プローブ。表示には影響しない。
// MK 未ロード時 / 監査代理閲覧時 / 科目未選択時は skip。


export function compareLedger(serverRows, jsResult) {
  // serverRows: [{entry_id, debit, credit, balance}]
  // jsResult.rows: [{entry_id, debit, credit, balance}]
  const byJs = new Map(jsResult.rows.map((r) => [r.entry_id, r]));
  const diffs = [];
  for (const sv of serverRows) {
    const js = byJs.get(sv.entry_id);
    if (!js) {
      diffs.push({ entry_id: sv.entry_id, kind: "missing_in_client" });
      continue;
    }
    if (js.debit !== sv.debit
        || js.credit !== sv.credit
        || js.balance !== sv.balance) {
      diffs.push({
        entry_id: sv.entry_id, kind: "mismatch",
        server: { debit: sv.debit, credit: sv.credit, balance: sv.balance },
        client: { debit: js.debit, credit: js.credit, balance: js.balance },
      });
    }
  }
  const serverIds = new Set(serverRows.map((r) => r.entry_id));
  for (const r of jsResult.rows) {
    if (!serverIds.has(r.entry_id)) {
      diffs.push({ entry_id: r.entry_id, kind: "extra_in_client", client: r });
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


async function _run() {
  const paramsEl = document.getElementById("ledger-server-params");
  if (!paramsEl) return;  // 科目未選択時はそもそも JSON が出ない

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("ledger_validator: failed to parse server params", e);
    return;
  }
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;
  if (!params.account_code) return;
  if (params.normal_balance !== "debit" && params.normal_balance !== "credit") {
    return;
  }

  const serverRows = [];
  document.querySelectorAll("[data-ledger-entry]").forEach((tr) => {
    serverRows.push({
      entry_id: parseInt(tr.getAttribute("data-ledger-entry") || "0", 10),
      debit: parseInt(tr.getAttribute("data-server-debit") || "0", 10),
      credit: parseInt(tr.getAttribute("data-server-credit") || "0", 10),
      balance: parseInt(tr.getAttribute("data-server-balance") || "0", 10),
    });
  });
  if (serverRows.length === 0) return;

  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeLedger }]
    = await Promise.all([
      import("/static/js/crypto/shared-client.js"),
      import("/static/js/crypto/journals_client.js"),
      import("/static/js/crypto/reports/ledger.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("ledger_validator: MK locked, skipping");
      return;
    }
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeLedger(entries, {
      accountCode: params.account_code,
      normalBalance: params.normal_balance,
      openingBalance: params.carry_forward,
      fiscalPeriodFrom: params.pf,
      fiscalPeriodTo: params.pt,
      includeClosing: true,
    });

    // computeLedger は asc 順で返す。サーバが desc の場合はサーバ側を asc に
    // 並べ直してから比較 (entry_id 順で OK)
    const sortedServer = params.sort === "desc"
      ? [...serverRows].sort((a, b) => a.entry_id - b.entry_id)
      : serverRows;

    const diffs = compareLedger(sortedServer, jsResult);
    if (diffs.length === 0) {
      console.info(
        `%c✓ ledger[${params.account_code}]: server vs client ${serverRows.length} 行一致 (closing balance ¥${jsResult.closing_balance.toLocaleString()})`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ ledger[${params.account_code}]: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("ledger_validator: error", e);
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
