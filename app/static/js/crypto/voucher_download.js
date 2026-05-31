// E4 (#111): 証憑暗号文の fetch + クライアント復号。
//
// サーバの画像配信エンドポイント (/ai-journal/voucher/<id>/image) は暗号化証憑を
// `iv(12B) || ciphertext || GCM tag` の opaque blob (application/octet-stream)
// として返す。本モジュールはそれを fetch し、AAD (vimg / vthumb + user_id +
// voucher_id) を組んで MK で復号し、平文画像バイト列を返す。
//
// DOM (URL.createObjectURL / <img>) は呼び出し側 (vouchers/*.mjs, app.js) が
// 担当し、本モジュールは Node 単体テスト可能な純ロジックに保つ。
//
// 設計書 §13.2 参照。

import { buildAAD } from "./record.js";


const _GCM_OVERHEAD = 12 + 16;  // iv + tag


/** 暗号化証憑画像の配信 URL。voucherId は URL sink なので明示サニタイズ。 */
export function voucherImageUrl(voucherId, thumb) {
  const id = encodeURIComponent(String(voucherId));
  return "/ai-journal/voucher/" + id + "/image" + (thumb ? "?size=thumb" : "");
}


/**
 * 暗号化証憑 (本体 or サムネ) を fetch + 復号して平文バイト列を返す。
 *
 * @param {Object} args
 * @param {Object} args.client            SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number|bigint} args.voucherId
 * @param {boolean} [args.thumb=false]    true ならサムネ (?size=thumb, vthumb AAD)
 * @param {Function} [args.fetchImpl]     テスト DI
 * @returns {Promise<Uint8Array>} 平文画像バイト列
 */
export async function fetchAndDecryptVoucherImage({
  client, userId, voucherId, thumb = false, fetchImpl,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f(voucherImageUrl(voucherId, thumb), {
    credentials: "include",
  });
  if (!r.ok) {
    throw new Error(`fetchAndDecryptVoucherImage: HTTP ${r.status}`);
  }
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf.byteLength < _GCM_OVERHEAD) {
    throw new Error("fetchAndDecryptVoucherImage: ciphertext too short");
  }
  const iv = buf.slice(0, 12);
  const ct = buf.slice(12);
  const aad = buildAAD(thumb ? "vthumb" : "vimg", userId, voucherId);
  const res = await client.decrypt(ct, iv, aad);
  return res.plaintext;
}
