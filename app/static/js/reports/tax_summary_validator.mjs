// 確定申告控除集計のサーバ集計 (HTML 描画値) とクライアント集計
// (computeTaxSummary) を並列実行して比較する fail-soft プローブ。
// 表示には影響しない。MK 未ロード時 / 監査代理閲覧時 / 控除行ゼロ件時は skip。
//
// medical / resident_tax は computeTaxSummary 側でも EXCLUDED として除外
// されているため、validator もそれらカテゴリには触らない。


export function compareTaxSummary(serverCats, jsResult) {
  // serverCats: [{cat, total, accounts: [{code, amount}]}]
  // jsResult: {[cat]: {total, accounts: [{code, amount}]}}  (computeTaxSummary 戻り値を
  //   accounts 内 code 付きに整形済み)
  const diffs = [];

  for (const sv of serverCats) {
    const js = jsResult[sv.cat];
    if (!js) {
      if (sv.total !== 0) {
        diffs.push({ cat: sv.cat, kind: "category_missing_in_client" });
      }
      continue;
    }
    if (js.total !== sv.total) {
      diffs.push({
        cat: sv.cat, kind: "category_total_mismatch",
        server: { total: sv.total },
        client: { total: js.total },
      });
    }
    const byJsCode = new Map(js.accounts.map((a) => [a.code, a]));
    for (const svAccount of sv.accounts) {
      const jsAccount = byJsCode.get(svAccount.code);
      if (!jsAccount) {
        if (svAccount.amount !== 0) {
          diffs.push({
            cat: sv.cat, code: svAccount.code,
            kind: "account_missing_in_client",
          });
        }
        continue;
      }
      if (jsAccount.amount !== svAccount.amount) {
        diffs.push({
          cat: sv.cat, code: svAccount.code,
          kind: "account_mismatch",
          server: { amount: svAccount.amount },
          client: { amount: jsAccount.amount },
        });
      }
    }
    const serverCodes = new Set(sv.accounts.map((a) => a.code));
    for (const ja of js.accounts) {
      if (!serverCodes.has(ja.code) && ja.amount !== 0) {
        diffs.push({
          cat: sv.cat, code: ja.code,
          kind: "account_extra_in_client",
          client: { amount: ja.amount },
        });
      }
    }
  }

  const serverCatNames = new Set(serverCats.map((c) => c.cat));
  for (const [cat, js] of Object.entries(jsResult)) {
    if (!serverCatNames.has(cat) && js.total !== 0) {
      diffs.push({ cat, kind: "category_extra_in_client", client: js });
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
  const paramsEl = document.getElementById("tax-summary-server-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("tax_summary_validator: failed to parse server params", e);
    return;
  }
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;
  if (typeof params.year !== "number") return;

  // サーバ表示行から serverCats + taxCategoryByCode を構築
  const serverCatsMap = new Map();
  const taxCategoryByCode = {};
  document.querySelectorAll("[data-tax-row]").forEach((tr) => {
    const code = tr.getAttribute("data-tax-row");
    const cat = tr.getAttribute("data-tax-row-category");
    const amount = parseInt(tr.getAttribute("data-server-amount") || "0", 10);
    if (!code || !cat) return;
    taxCategoryByCode[code] = cat;
    let bucket = serverCatsMap.get(cat);
    if (!bucket) {
      bucket = { cat, total: 0, accounts: [] };
      serverCatsMap.set(cat, bucket);
    }
    bucket.accounts.push({ code, amount });
  });
  // カテゴリ total は data-server-total から取得
  document.querySelectorAll("[data-tax-category]").forEach((card) => {
    const cat = card.getAttribute("data-tax-category");
    const total = parseInt(card.getAttribute("data-server-total") || "0", 10);
    const bucket = serverCatsMap.get(cat);
    if (bucket) bucket.total = total;
  });

  const serverCats = [...serverCatsMap.values()];
  if (serverCats.length === 0) return;

  const [{ SharedCryptoClient }, { fetchJournalsForYear }, { computeTaxSummary }]
    = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/tax_summary.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("tax_summary_validator: MK locked, skipping");
      return;
    }
    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    // computeTaxSummary は name しか返さないが、validator は code で比較する
    // ため、accountNameByCode の逆で code → 結果オブジェクト構造に再生成する
    // 必要がある。computeTaxSummary に accountNameByCode を渡さず、code を
    // そのまま name フィールドに使わせてから code フィールドを後付けする
    // 戦略を取る (entries 内に line.account_code 情報がそのまま含まれる)
    const jsRaw = computeTaxSummary(entries, { taxCategoryByCode });
    const jsResult = {};
    for (const [cat, data] of Object.entries(jsRaw)) {
      jsResult[cat] = {
        total: data.total,
        // computeTaxSummary の accounts は name を持つが、accountNameByCode 未指定
        // のため name === code になっている。それを code フィールドにリネーム。
        accounts: data.accounts.map((a) => ({
          code: a.name, amount: a.amount,
        })),
      };
    }
    const diffs = compareTaxSummary(serverCats, jsResult);
    if (diffs.length === 0) {
      console.info(
        `%c✓ tax_summary: server vs client ${serverCats.length} カテゴリ一致`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ tax_summary: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("tax_summary_validator: error", e);
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
