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
    const blob = b64decode(apiEntry.encrypted_blob);
    const iv = b64decode(apiEntry.blob_iv);
    const aad = buildAAD("je", userId, apiEntry.id);
    body = await decryptRecord(client, blob, iv, aad);
  }
  const lines = await Promise.all(
    (apiEntry.lines || []).map((line, idx) =>
      _normalizeLine(client, userId, apiEntry.id, line, idx),
    ),
  );
  return {
    id: apiEntry.id,
    fiscal_year: apiEntry.fiscal_year,
    // 復号できれば暗号化された値、なければ平文フォールバック
    date: body?.date ?? apiEntry.date,
    description: body?.description ?? apiEntry.description,
    source: body?.source ?? apiEntry.source,
    batch_id: body?.batch_id ?? null,
    fiscal_period: body?.fiscal_period ?? null,
    lines,
  };
}


async function _normalizeLine(client, userId, entryId, apiLine, lineIdx) {
  // line_id は API レスポンスに含まれない。API 側で line_id を返すよう
  // E3-B 時点で拡張すべきだったが (申し送り)、暫定で line index (0-based)
  // を AAD に使う。E3 完了前に line_id 返却に移行する必要あり。
  // (本実装は test での round-trip 用ダミー実装で、本番 production では
  //  サーバ側で line_id を返却することを前提とする)
  let body = null;
  if (apiLine.encrypted_blob && apiLine.blob_iv) {
    const blob = b64decode(apiLine.encrypted_blob);
    const iv = b64decode(apiLine.blob_iv);
    // 暫定: line.id があれば使う、なければ lineIdx (TODO: API 側で line.id を返す)
    const lineId = apiLine.id ?? lineIdx;
    const aad = buildAAD("jel", userId, entryId, lineId);
    body = await decryptRecord(client, blob, iv, aad);
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
    if (all.length >= (body.total || 0) || journals.length === 0) break;
    page += 1;
    if (page > 1000) {
      throw new Error("fetchJournalsForYear: pagination exceeded 1000 pages");
    }
  }
  return all;
}
