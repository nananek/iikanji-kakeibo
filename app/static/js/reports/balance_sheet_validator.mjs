// 貸借対照表 (B/S) のサーバ集計とクライアント集計 (computeBalanceSheet) を
// 並列実行して比較する fail-soft プローブ。表示には影響しない。
// MK 未ロード時 / 監査代理閲覧時 / 仕訳ゼロ件時は skip。
//
// B/S は累計のため、min_year..year の各年度を fetch して entries を結合する。
// 過去年度 closing 仕訳は source を "_closing_past" に書き換えて、
// computeBalanceSheet の hasClosing 判定 (当年度の closing のみで判定する
// サーバ挙動と一致させたい) から外す。集計には残るため累計値は正しい。


export function compareBalanceSheet(serverRows, serverTotals, jsResult) {
  // serverRows: [{code, section: "assets"|"liabilities"|"equities", balance}]
  // serverTotals: {assets, liabilities, equity, net_income}
  // jsResult: { assets, liabilities, equities, total_assets, total_liabilities,
  //             total_equity, net_income, has_closing }
  const diffs = [];

  if (jsResult.total_assets !== serverTotals.assets) {
    diffs.push({
      kind: "total_assets_mismatch",
      server: serverTotals.assets, client: jsResult.total_assets,
    });
  }
  if (jsResult.total_liabilities !== serverTotals.liabilities) {
    diffs.push({
      kind: "total_liabilities_mismatch",
      server: serverTotals.liabilities, client: jsResult.total_liabilities,
    });
  }
  // server 側は has_closing=false のときに total_equity に net_income を加算済み。
  // computeBalanceSheet も同じ仕様なので、純資産は加算済み値を比較する。
  if (jsResult.total_equity + (jsResult.has_closing ? 0 : jsResult.net_income)
      !== serverTotals.equity) {
    diffs.push({
      kind: "total_equity_mismatch",
      server: serverTotals.equity,
      client: jsResult.total_equity
        + (jsResult.has_closing ? 0 : jsResult.net_income),
    });
  }

  // 行レベル比較 — section ごとに別 Map を用意 (同 code が複数 section に
  // 出現することは設計上ないが、念のため section も鍵に含める)
  const byJs = new Map();
  for (const r of jsResult.assets) byJs.set(`assets:${r.account_code}`, r);
  for (const r of jsResult.liabilities) byJs.set(`liabilities:${r.account_code}`, r);
  for (const r of jsResult.equities) byJs.set(`equities:${r.account_code}`, r);

  for (const sv of serverRows) {
    const key = `${sv.section}:${sv.code}`;
    const js = byJs.get(key);
    if (!js) {
      if (sv.balance !== 0) {
        diffs.push({ code: sv.code, section: sv.section, kind: "missing_in_client" });
      }
      continue;
    }
    if (js.balance !== sv.balance) {
      diffs.push({
        code: sv.code, section: sv.section, kind: "balance_mismatch",
        server: sv.balance, client: js.balance,
      });
    }
  }
  const serverKeys = new Set(serverRows.map((r) => `${r.section}:${r.code}`));
  for (const [key, js] of byJs) {
    if (!serverKeys.has(key) && js.balance !== 0) {
      const [section, code] = key.split(":");
      diffs.push({ code, section, kind: "extra_in_client", client: js.balance });
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
  const paramsEl = document.getElementById("bs-server-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("balance_sheet_validator: failed to parse server params", e);
    return;
  }
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;
  if (typeof params.year !== "number") return;
  if (typeof params.min_year !== "number") return;  // 仕訳ゼロ件なら null → skip

  const serverRows = [];
  const accountTypeByCode = {};
  const normalBalanceByCode = {};
  document.querySelectorAll("[data-bs-row]").forEach((tr) => {
    const code = tr.getAttribute("data-bs-row");
    const section = tr.getAttribute("data-bs-section");
    const normal = tr.getAttribute("data-bs-normal");
    const balance = parseInt(tr.getAttribute("data-server-balance") || "0", 10);
    if (!code || !section) return;
    serverRows.push({ code, section, balance });
    accountTypeByCode[code] = section === "assets"
      ? "asset"
      : (section === "liabilities" ? "liability" : "equity");
    if (normal === "debit" || normal === "credit") {
      normalBalanceByCode[code] = normal;
    }
  });
  if (serverRows.length === 0) return;

  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeBalanceSheet }]
    = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/balance_sheet.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("balance_sheet_validator: MK locked, skipping");
      return;
    }

    // 複数年度を順次 fetch (B/S は累計のため)
    const allEntries = [];
    for (let y = params.min_year; y <= params.year; y++) {
      const ye = await fetchJournalsForYear({
        client, userId: params.user_id, fiscalYear: y,
      });
      for (const e of ye) {
        // 過去年度 closing は has_closing 判定から外す
        // (サーバ側 bs() は当年度の closing のみで has_closing を判定するため)。
        // 集計には残るので累計値は正しい。
        if (e.fiscal_year !== params.year && e.source === "closing") {
          allEntries.push({ ...e, source: "_closing_past" });
        } else {
          allEntries.push(e);
        }
      }
    }

    const jsResult = computeBalanceSheet(allEntries, {
      accountTypeByCode, normalBalanceByCode,
    });

    const diffs = compareBalanceSheet(
      serverRows,
      params.totals,
      jsResult,
    );
    if (diffs.length === 0) {
      console.info(
        `%c✓ balance_sheet: server vs client ${serverRows.length} 行一致 (${params.year - params.min_year + 1} 年度 fetch)`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ balance_sheet: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("balance_sheet_validator: error", e);
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
