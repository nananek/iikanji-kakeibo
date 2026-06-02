// E4 (#111): 証憑暗号文の fetch + クライアント復号。
//
// サーバの画像配信エンドポイント (/ai-journal/voucher/<id>/image) は暗号化証憑を
// `iv(12B) || ciphertext || GCM tag` の opaque blob (application/octet-stream)
// として返す。本モジュールはそれを fetch し、AAD (vimg / vthumb + user_id +
// aad_id) を組んで MK で復号し、平文画像バイト列を返す。aad_id は voucher_id と
// 独立した安定識別子 (E4 #111 Option C) で、URL/fetch には voucher_id、AAD 束縛
// には aad_id を使う (backup/restore の PK 再採番後も復号できるようにするため)。
//
// DOM (URL.createObjectURL / <img>) は呼び出し側 (vouchers/*.mjs, app.js) が
// 担当し、本モジュールは Node 単体テスト可能な純ロジックに保つ。
//
// 設計書 §13.2 参照。

import { buildAAD } from "./record.js";


const _GCM_OVERHEAD = 12 + 16;  // iv + tag


/**
 * 復号した平文画像の先頭バイト (マジックナンバー) から MIME を判定する。
 *
 * 暗号化証憑の元 MIME は encrypted_meta_blob 内にあるが、画像表示には magic
 * byte からの判定で十分 (アップロード許可は jpeg/png/webp/gif に限定)。判定
 * 不能なら application/octet-stream を返す (<img> は content sniffing で表示)。
 *
 * @param {Uint8Array} b
 * @returns {string}
 */
export function sniffImageMime(b) {
  if (!b || b.length < 4) return "application/octet-stream";
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return "image/jpeg";
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) {
    return "image/png";
  }
  if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) return "image/gif";
  if (
    b.length >= 12 &&
    b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 &&
    b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50
  ) {
    return "image/webp";
  }
  return "application/octet-stream";
}


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
 * @param {number|bigint} args.voucherId  URL/fetch 用 (storage key)
 * @param {number|bigint} args.aadId      AAD 束縛用安定識別子 (vimg/vthumb)
 * @param {boolean} [args.thumb=false]    true ならサムネ (?size=thumb, vthumb AAD)
 * @param {Function} [args.fetchImpl]     テスト DI
 * @returns {Promise<Uint8Array>} 平文画像バイト列
 */
export async function fetchAndDecryptVoucherImage({
  client, userId, voucherId, aadId, thumb = false, fetchImpl,
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
  return decryptVoucherBlob({ client, userId, aadId, blob: buf, thumb });
}


/**
 * 既に手元にある opaque blob (iv(12B) || ciphertext || GCM tag) を AAD を組んで
 * MK 復号し、平文画像バイト列を返す。配信エンドポイントから取得済みの blob や、
 * backup export の image_data を再 fetch せず復号する用途 (監査 Lv3 スナップショット
 * の証憑同梱など) に使う。
 *
 * @param {Object} args
 * @param {Object} args.client            SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number|bigint} args.aadId      AAD 束縛用安定識別子 (vimg/vthumb)
 * @param {Uint8Array} args.blob          iv(12B) || ciphertext || tag
 * @param {boolean} [args.thumb=false]
 * @returns {Promise<Uint8Array>} 平文画像バイト列
 */
export async function decryptVoucherBlob({ client, userId, aadId, blob, thumb = false }) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (!(blob instanceof Uint8Array) || blob.byteLength < _GCM_OVERHEAD) {
    throw new Error("decryptVoucherBlob: ciphertext too short");
  }
  const iv = blob.slice(0, 12);
  const ct = blob.slice(12);
  const aad = buildAAD(thumb ? "vthumb" : "vimg", userId, aadId);
  const res = await client.decrypt(ct, iv, aad);
  return res.plaintext;
}
