// Phase E3-E-2: balance_cache_blobs クライアント helper。
//
// /api/v1/balance-cache-blobs (PR #215) を叩いて、(year, period) ごとの
// {accountCode: [debit, credit]} を MK で暗号化/復号する wrapper。
//
// 呼出側 (B/S validator や IndexedDB ストレージ層) はこの helper だけ使えば
// 暗号文と AAD の組み立てを意識せずに済む。
//
// AAD: buildAAD("bcb", userId, year*100 + period)
// Payload: JSON.stringify({accountCode: [debit, credit], ...})

import { buildAAD } from "./record.js";
import { b64encode, b64decode } from "./b64.js";


const TEXT_ENC = new TextEncoder();
const TEXT_DEC = new TextDecoder();


function _csrf() {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


function _periodKey(year, period) {
  // AAD 用の (year, period) コンパクトエンコード。0-16 を 100 倍シフトで
  // 重複しない 1 整数にする (year * 100 + period < 2^63 で安全)。
  return year * 100 + period;
}


function _validateUserId(userId) {
  if (typeof userId !== "number" && typeof userId !== "bigint") {
    throw new Error("userId must be a number or bigint");
  }
  if (typeof userId === "number" && !Number.isSafeInteger(userId)) {
    throw new Error("userId Number must be a safe integer (use BigInt for > 2^53)");
  }
}


function _validateYear(year) {
  if (!Number.isInteger(year) || !(1900 <= year && year <= 2200)) {
    throw new Error("year must be int in 1900..2200");
  }
}


function _validatePeriod(period) {
  if (!Number.isInteger(period) || !(0 <= period && period <= 16)) {
    throw new Error("period must be int in 0..16");
  }
}


/**
 * 指定年度の balance_cache_blobs を取得して MK で復号、
 * {period: {accountCode: [debit, credit]}} を返す。
 * 復号失敗 (MK 変更後等) の blob は skip。
 *
 * @param {Object} args
 * @param {Object} args.client            SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number} args.fiscalYear
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<Object<number, Object<string, [number, number]>>>}
 */
export async function fetchBalanceCacheBlobs({
  client, userId, fiscalYear, fetchImpl,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  _validateUserId(userId);
  _validateYear(fiscalYear);
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f(`/api/v1/balance-cache-blobs?year=${fiscalYear}`, {
    credentials: "include",
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(
      `fetchBalanceCacheBlobs: HTTP ${r.status} ${e.error || ""}`,
    );
  }
  const body = await r.json();
  const blobs = body.blobs || [];
  const result = {};
  for (const b of blobs) {
    if (b.year !== fiscalYear) continue;
    if (!Number.isInteger(b.period)) continue;
    let blobBytes, ivBytes;
    try {
      blobBytes = b64decode(b.encrypted_blob);
      ivBytes = b64decode(b.blob_iv);
    } catch (_e) {
      continue;
    }
    const aad = buildAAD("bcb", userId, _periodKey(fiscalYear, b.period));
    let plain;
    try {
      const dec = await client.decrypt(blobBytes, ivBytes, aad);
      plain = dec.plaintext;
    } catch (_e) {
      // 復号失敗 (MK 変更後等) は skip して残りを処理。
      continue;
    }
    try {
      const json = TEXT_DEC.decode(plain);
      const obj = JSON.parse(json);
      if (obj && typeof obj === "object") {
        result[b.period] = obj;
      }
    } catch (_e) {
      // JSON 不正は skip。
    } finally {
      try { plain.fill(0); } catch (_e) { /* ignore */ }
    }
  }
  return result;
}


/**
 * (year, period) の balance cache を暗号化して PUT。
 *
 * @param {Object} args
 * @param {Object} args.client            SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number} args.year
 * @param {number} args.period            0..16
 * @param {Object<string, [number, number]>} args.balances
 *   {accountCode: [debit, credit]} の plain object
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{ok: boolean, updated_at: string}>}
 */
export async function saveBalanceCacheBlob({
  client, userId, year, period, balances, fetchImpl,
}) {
  if (!client || typeof client.encrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  _validateUserId(userId);
  _validateYear(year);
  _validatePeriod(period);
  if (!balances || typeof balances !== "object") {
    throw new TypeError("balances must be an object");
  }
  const f = fetchImpl ?? globalThis.fetch;
  const json = JSON.stringify(balances);
  const plain = TEXT_ENC.encode(json);
  const aad = buildAAD("bcb", userId, _periodKey(year, period));
  const enc = await client.encrypt(plain, aad);
  const r = await f(
    `/api/v1/balance-cache-blobs/${year}/${period}`,
    {
      method: "PUT",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": _csrf(),
      },
      body: JSON.stringify({
        encrypted_blob: b64encode(enc.ciphertext),
        blob_iv: b64encode(enc.iv),
      }),
    },
  );
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(
      `saveBalanceCacheBlob: HTTP ${r.status} ${e.error || ""}`,
    );
  }
  return r.json();
}


/**
 * 指定年 (またはそれ以降の period) の balance cache を削除。
 * 月次確定解除時にクライアントから呼ぶ想定。
 *
 * @param {Object} args
 * @param {number} args.year
 * @param {number} [args.fromPeriod]      指定すると >= fromPeriod の period だけ削除
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{ok: boolean, deleted: number}>}
 */
export async function deleteBalanceCacheBlobs({
  year, fromPeriod, fetchImpl,
}) {
  _validateYear(year);
  const f = fetchImpl ?? globalThis.fetch;
  let url = `/api/v1/balance-cache-blobs/${year}`;
  if (fromPeriod !== undefined && fromPeriod !== null) {
    _validatePeriod(fromPeriod);
    url += `?from_period=${fromPeriod}`;
  }
  const r = await f(url, {
    method: "DELETE",
    credentials: "include",
    headers: { "X-CSRFToken": _csrf() },
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(
      `deleteBalanceCacheBlobs: HTTP ${r.status} ${e.error || ""}`,
    );
  }
  return r.json();
}
