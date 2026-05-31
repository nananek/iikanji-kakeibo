// Phase E3-F PR-D-6-3b-2: クライアント側 インポートバッチ一覧 取得 + 復号 helper。
//
// `GET /api/v1/journals/batches` を 1 回叩き、バッチ (batch_id) ごとの保持列
// メタ (件数 / 取込日時 / 削除可否) と各仕訳の encrypted_blob / blob_iv を取得
// する。entry blob を MK で復号して種別ラベル (source) と日付範囲
// (date_from / date_to) をクライアント側で組み立てる (平文 date / source は
// D-6-5 で DROP 予定のためサーバ側では読まない)。
//
// closing (損益振替・自動生成) 仕訳はサーバが MK を持たず暗号化できないため
// encrypted_blob が空 = body は null。is_closing / fiscal_year の保持列から
// date (年末 12/31) / source ("closing") を合成する (journals_client.js
// _normalizeEntry と一致)。
//
// 注: lines は取得しない (バッチ一覧に不要)。entry-level blob のみ復号する。

import { b64decode } from "./b64.js";
import { buildAAD, decryptRecord } from "./record.js";


/**
 * 1 バッチを正規化 (entry blob を復号して source / 日付範囲を導出)。
 *
 * @returns {Promise<Object>} {batch_id, source, count, imported_at,
 *   is_closing, date_from, date_to, deletable, delete_reason}
 */
async function _normalizeBatch(client, userId, b) {
  // 非 closing バッチは全 entry が同一 source を持つ (batch_id は取込操作単位
  // で採番されるため均質)。最初に復号できた entry の source を採用する。
  let source = b.is_closing ? "closing" : "";
  let dateFrom = null;
  let dateTo = null;

  for (const e of b.entries || []) {
    let body = null;
    if (e.encrypted_blob && e.blob_iv) {
      try {
        const blob = b64decode(e.encrypted_blob);
        const iv = b64decode(e.blob_iv);
        const aad = buildAAD("je", userId);
        body = await decryptRecord(client, blob, iv, aad);
      } catch (err) {
        // 復号失敗 (AAD 不一致 / 鍵不一致) は 1 件単位で局所化する。
        console.warn(
          `batches_client: entry ${e.id} decrypt failed: ${err?.message || err}`,
        );
      }
    }
    // closing 仕訳は保持列から合成 (journals_client _normalizeEntry と一致)。
    const isClosing = e.is_closing ?? false;
    const date = body?.date ?? (isClosing ? `${e.fiscal_year}-12-31` : null);
    const src = body?.source ?? (isClosing ? "closing" : "");
    if (src && !source) source = src;
    // ISO 日付文字列 (YYYY-MM-DD) は辞書順比較で日付順と一致する。
    if (date) {
      if (dateFrom === null || date < dateFrom) dateFrom = date;
      if (dateTo === null || date > dateTo) dateTo = date;
    }
  }

  return {
    batch_id: b.batch_id,
    source,
    count: b.count,
    imported_at: b.imported_at ?? null,
    is_closing: !!b.is_closing,
    date_from: dateFrom,
    date_to: dateTo,
    deletable: !!b.deletable,
    delete_reason: b.delete_reason || "",
  };
}


/**
 * 全インポートバッチを取得 + 復号。
 *
 * @param {Object} args
 * @param {Object} args.client          SharedCryptoClient (decrypt 用)
 * @param {number|bigint} args.userId   復号 AAD に使う
 * @param {Function} [args.fetchImpl]   テスト DI
 * @returns {Promise<Array<Object>>}    正規化されたバッチ配列 (新しい順)
 */
export async function fetchBatches({ client, userId, fetchImpl }) {
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
  const f = fetchImpl ?? globalThis.fetch;

  const r = await f("/api/v1/journals/batches", { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`fetchBatches: HTTP ${r.status} ${e.error || ""}`);
  }
  const body = await r.json();
  const out = [];
  for (const b of body.batches || []) {
    out.push(await _normalizeBatch(client, userId, b));
  }
  return out;
}
