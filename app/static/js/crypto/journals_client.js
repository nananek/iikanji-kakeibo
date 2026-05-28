// Phase E3-C: クライアント側 仕訳 (JournalEntry) 取得 + 復号 helper。
//
// `GET /api/v1/journals?fiscal_year=YYYY` をページネーション全件取得し、
// レスポンスの encrypted_blob / blob_iv を MK で復号して plain object 配列を
// 返す。dual read 期間中は blob/iv が null の (未移行) 行もサポートし、
// 旧平文フィールド (date / description / lines[].account_code 等) で
// 同じ shape の object を組み立てる。
//
// これがクライアント側レポート (試算表 / P/L / B/S / etc, E3-C-2〜) の
// データソース基盤。設計書 §12.3 / §12.9 参照。

import { b64decode } from "./b64.js";
import { buildAAD, decryptRecord } from "./record.js";


/**
 * 1 つの API entry を復号 (または平文) して正規化形式に変換。
 *
 * 復号できない場合 (AAD 不一致等) や blob/iv が null の場合は旧平文
 * フィールドを使う。lines も同様。
 *
 * @returns {Promise<Object>} {id, fiscal_year, date, description, source, lines: [{account_code, debit, credit, description}]}
 */
async function _normalizeEntry(client, userId, apiEntry) {
  let body = null;
  if (apiEntry.encrypted_blob && apiEntry.blob_iv) {
    try {
      const blob = b64decode(apiEntry.encrypted_blob);
      const iv = b64decode(apiEntry.blob_iv);
      const aad = buildAAD("je", userId);
      body = await decryptRecord(client, blob, iv, aad);
    } catch (e) {
      // 復号失敗 (AAD 不一致 / bit flip / 鍵不一致) は dual-read 設計通り
      // 平文フォールバックする。1 件の異常で全件取得が失敗しないよう
      // entry 単位で局所化する。
      console.warn(
        `journals_client: entry ${apiEntry.id} decrypt failed, ` +
        `falling back to plaintext: ${e?.message || e}`,
      );
    }
  }
  // _normalizeLine 内の throw (apiLine.id 欠落等) が Promise.all を突き抜け
  // て fetchJournalsForYear 全体を reject させないよう、line 単位で catch して
  // 平文フォールバックに局所化する (entry の try/catch と同じ方針)。
  const lines = await Promise.all(
    (apiEntry.lines || []).map((line) =>
      _normalizeLine(client, userId, apiEntry.id, line).catch((e) => {
        console.warn(
          `journals_client: line normalization failed ` +
          `(entry=${apiEntry.id}): ${e?.message || e}`,
        );
        return {
          account_code: line.account_code ?? null,
          debit: line.debit ?? 0,
          credit: line.credit ?? 0,
          description: line.description ?? "",
        };
      }),
    ),
  );
  return {
    id: apiEntry.id,
    fiscal_year: apiEntry.fiscal_year,
    // 復号できれば暗号化された値、なければ平文フォールバック
    date: body?.date ?? apiEntry.date,
    description: body?.description ?? apiEntry.description,
    source: body?.source ?? apiEntry.source,
    // batch_id も fiscal_period と同じ 3 段フォールバック (将来 API が
    // batch_id を返した時の dual-read 平文行に備える)
    batch_id: body?.batch_id ?? apiEntry.batch_id ?? null,
    // fiscal_period: 復号値 → API レスポンスの平文 → null の優先順
    // (API 側 _entry_to_dict が fiscal_period を返却するよう E3-C-1b で拡張)
    fiscal_period: body?.fiscal_period ?? apiEntry.fiscal_period ?? null,
    lines,
  };
}


async function _normalizeLine(client, userId, entryId, apiLine) {
  let body = null;
  if (apiLine.encrypted_blob && apiLine.blob_iv) {
    try {
      const blob = b64decode(apiLine.encrypted_blob);
      const iv = b64decode(apiLine.blob_iv);
      // E3-F PR-A: AAD は Option B (user_id のみ)。entry_id / line_id swap
      // 検知能力は失う代わりに新規 POST 時の AAD 構築が単純化される。
      const aad = buildAAD("jel", userId);
      body = await decryptRecord(client, blob, iv, aad);
    } catch (e) {
      console.warn(
        `journals_client: line ${apiLine.id} (entry=${entryId}) decrypt ` +
        `failed, falling back to plaintext: ${e?.message || e}`,
      );
    }
  }
  return {
    account_code: body?.account_code ?? apiLine.account_code,
    debit: body?.debit_amount ?? apiLine.debit ?? 0,
    credit: body?.credit_amount ?? apiLine.credit ?? 0,
    description: body?.description ?? apiLine.description ?? "",
  };
}


/**
 * 指定年度の全仕訳を取得 + 復号。
 *
 * ページネーションを自動で回し全件取得する。100 件/ページで取得し、
 * total >= 累計取得数に達するまで繰り返す。
 *
 * @param {Object} args
 * @param {Object} args.client                       SharedCryptoClient (decrypt 用)
 * @param {number|bigint} args.userId                 復号 AAD に使う
 * @param {number} args.fiscalYear
 * @param {Function} [args.fetchImpl]                 テスト DI
 * @param {number} [args.perPage=100]
 * @returns {Promise<Array<Object>>}                  正規化された entry 配列
 */
export async function fetchJournalsForYear({
  client, userId, fiscalYear, fetchImpl, perPage = 100,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (userId === undefined || userId === null) {
    throw new Error("userId is required");
  }
  if (typeof userId !== "number" && typeof userId !== "bigint") {
    throw new Error("userId must be a number or bigint");
  }
  if (typeof userId === "number" && !Number.isSafeInteger(userId)) {
    throw new Error("userId Number must be a safe integer (use BigInt for > 2^53)");
  }
  if (!Number.isInteger(fiscalYear) || !(1900 <= fiscalYear && fiscalYear <= 2200)) {
    throw new Error("fiscalYear must be int in 1900..2200");
  }
  const f = fetchImpl ?? globalThis.fetch;

  const all = [];
  let page = 1;
  while (true) {
    const url = `/api/v1/journals?fiscal_year=${fiscalYear}&page=${page}&per_page=${perPage}`;
    const r = await f(url, { credentials: "include" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(`fetchJournalsForYear: HTTP ${r.status} ${e.error || ""}`);
    }
    const body = await r.json();
    const journals = body.journals || [];
    for (const apiEntry of journals) {
      all.push(await _normalizeEntry(client, userId, apiEntry));
    }
    // 打ち切り条件:
    //   1. 当該ページが空 → これ以上ない
    //   2. total が定義されており、累計取得数が total に達した
    // `body.total || 0` だと total=0 + journals 非空のサーババグ時に
    // 即 break して 2 ページ目以降を取得しないので、明示的に分離する。
    if (journals.length === 0) break;
    if (typeof body.total === "number" && all.length >= body.total) break;
    page += 1;
    if (page > 1000) {
      throw new Error("fetchJournalsForYear: pagination exceeded 1000 pages");
    }
  }
  return all;
}
