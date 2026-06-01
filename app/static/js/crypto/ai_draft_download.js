// E5 (#111): AI 下書き暗号文の fetch + クライアント復号。
//
// サーバの下書き画像配信 (/ai-journal/drafts/<id>/image) は暗号化下書きを
// `iv(12B) || ciphertext || GCM tag` の opaque blob (application/octet-stream)
// として返す。本モジュールはそれを fetch し、AAD (vimg / vthumb + user_id +
// aad_id、証憑と同ドメイン) を組んで MK で復号し、平文画像バイト列を返す。
//
// voucher_download と同形 (URL だけ下書きエンドポイントに差し替え)。MIME 判定
// (sniffImageMime) は voucher_download のものを再利用・再エクスポートする。
//
// DOM (URL.createObjectURL / <img>) は呼び出し側 (ai_journal/*.mjs) が担当し、
// 本モジュールは Node 単体テスト可能な純ロジックに保つ。
//
// 設計書 §13.6 参照。

import { buildAAD } from "./record.js";
import { sniffImageMime } from "./voucher_download.js";


const _GCM_OVERHEAD = 12 + 16;  // iv + tag


// 画像表示時の MIME 判定は証憑と共通 (magic byte ベース)。再エクスポートして
// 呼び出し側 (renderer) が 1 モジュール import で済むようにする。
export { sniffImageMime };


/** 暗号化下書き画像の配信 URL。draftId は URL sink なので明示サニタイズ。 */
export function draftImageUrl(draftId, thumb) {
  const id = encodeURIComponent(String(draftId));
  return "/ai-journal/drafts/" + id + "/image" + (thumb ? "?size=thumb" : "");
}


/**
 * 暗号化下書き (本体 or サムネ) を fetch + 復号して平文バイト列を返す。
 *
 * @param {Object} args
 * @param {Object} args.client            SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number|bigint} args.draftId    URL/fetch 用 (storage key)
 * @param {number|bigint} args.aadId      AAD 束縛用安定識別子 (vimg/vthumb)
 * @param {boolean} [args.thumb=false]    true ならサムネ (?size=thumb, vthumb AAD)
 * @param {Function} [args.fetchImpl]     テスト DI
 * @returns {Promise<Uint8Array>} 平文画像バイト列
 */
export async function fetchAndDecryptDraftImage({
  client, userId, draftId, aadId, thumb = false, fetchImpl,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f(draftImageUrl(draftId, thumb), {
    credentials: "include",
  });
  if (!r.ok) {
    throw new Error(`fetchAndDecryptDraftImage: HTTP ${r.status}`);
  }
  const buf = new Uint8Array(await r.arrayBuffer());
  if (buf.byteLength < _GCM_OVERHEAD) {
    throw new Error("fetchAndDecryptDraftImage: ciphertext too short");
  }
  const iv = buf.slice(0, 12);
  const ct = buf.slice(12);
  const aad = buildAAD(thumb ? "vthumb" : "vimg", userId, aadId);
  const res = await client.decrypt(ct, iv, aad);
  return res.plaintext;
}
