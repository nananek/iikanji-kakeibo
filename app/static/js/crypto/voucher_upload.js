// E4 (#111): 証憑画像のクライアント完結 E2EE upload。
//
// 2 段階 upload (Option A):
//   1. initVoucher()   → POST /api/v1/vouchers/init で voucher_id 採番 + aad_id 受領
//   2. encryptVoucher() → aad_id を AAD に束縛して画像/サムネ/メタを暗号化
//   3. putVoucher()    → PUT /api/v1/vouchers/<id> で暗号文の実体を upload
//
// E4 (#111) Option C: AAD は voucher_id ではなくサーバ生成の安定識別子 aad_id
// に束縛する。voucher_id は backup/restore で再採番されるため、AAD に使うと復元後
// に復号不能になる。aad_id は再採番後も保持されるため復号互換を保てる。aad_id は
// 63bit のためサーバは文字列で返し、クライアントは BigInt として AAD に渡す
// (uint64BE は BigInt 対応)。
//
// 画像/サムネ本体はストレージ保存のため `iv(12B) || ciphertext || GCM tag` の
// opaque blob として連結して送る (IV は DB 列ではなく blob 先頭に inline)。
// メタ (original_filename + image_mime 等) は DB 格納のため base64 で送り、
// IV は meta_iv フィールドで別送する。
//
// file_hash_plain (= SHA-256(平文画像)) はクライアントが計算して送信し、サーバ
// は file_hash_cipher (= SHA-256(暗号文)) を計算する (電帳法 Q11 ハイブリッド)。
//
// DOM (canvas) 依存のサムネイル生成は本モジュールに含めず、呼び出し側が
// `makeThumbnail` として注入する (Node 単体テスト可能にするため)。
//
// 設計書 §13.2 / §13.3 / §13.4 参照。

import { buildAAD } from "./record.js";
import { b64encode } from "./b64.js";


// 平文画像の上限 (サーバ vouchers.MAX_IMAGE_SIZE と一致)。
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;


function _csrf() {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


function _concat(a, b) {
  const out = new Uint8Array(a.byteLength + b.byteLength);
  out.set(a, 0);
  out.set(b, a.byteLength);
  return out;
}


function _toUint8(bytes) {
  if (bytes instanceof Uint8Array) return bytes;
  if (bytes instanceof ArrayBuffer) return new Uint8Array(bytes);
  throw new TypeError("bytes must be Uint8Array or ArrayBuffer");
}


/** SHA-256 を hex 文字列 (64 桁) で返す。 */
export async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", _toUint8(bytes));
  const arr = new Uint8Array(digest);
  let hex = "";
  for (let i = 0; i < arr.length; i++) {
    hex += arr[i].toString(16).padStart(2, "0");
  }
  return hex;
}


/** 生バイト列を MK で暗号化し、`iv || ciphertext` の opaque blob を返す。 */
async function _encryptBlob(client, bytes, aad) {
  const enc = await client.encrypt(bytes, aad);
  return _concat(enc.iv, enc.ciphertext);
}


/**
 * Step 1: 空 Voucher を作成して voucher_id を採番する。
 *
 * @param {Object} args
 * @param {number|null} [args.journalEntryId]  紐付け仕訳 id (孤立証憑なら null)
 * @param {Function} [args.fetchImpl]           テスト DI
 * @param {string} [args.csrf]                  CSRF トークン (省略時は meta tag)
 * @returns {Promise<{voucherId: number, aadId: bigint}>}
 *   voucherId は URL/storage 用、aadId は AAD 束縛用 (BigInt)。
 */
export async function initVoucher({ journalEntryId = null, fetchImpl, csrf } = {}) {
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f("/api/v1/vouchers/init", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrf ?? _csrf(),
    },
    body: JSON.stringify({ journal_entry_id: journalEntryId }),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`initVoucher: HTTP ${r.status} ${e.error || ""}`);
  }
  const data = await r.json();
  // aad_id は文字列で返る (63bit, JS Number 精度対策) → BigInt にパース。
  return { voucherId: data.voucher_id, aadId: BigInt(data.aad_id) };
}


/**
 * Step 2: 画像/サムネ/メタを採番済み voucher_id に束縛して暗号化する。
 *
 * @param {Object} args
 * @param {Object} args.client                SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {number|bigint} args.aadId          AAD 束縛用安定識別子 (init で受領)
 * @param {Uint8Array|ArrayBuffer} args.imageBytes  平文画像バイト列
 * @param {Uint8Array|ArrayBuffer|null} [args.thumbBytes]  平文サムネ (任意)
 * @param {Object} [args.meta]                メタ情報 (original_filename 等)
 * @returns {Promise<{imageCt: Uint8Array, thumbCt: Uint8Array|null,
 *   metaBlob: Uint8Array, metaIv: Uint8Array, fileHashPlain: string}>}
 */
export async function encryptVoucher({
  client, userId, aadId, imageBytes, thumbBytes = null, meta = {},
}) {
  if (!client || typeof client.encrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const image = _toUint8(imageBytes);
  if (image.byteLength === 0) {
    throw new Error("imageBytes is empty");
  }
  if (image.byteLength > MAX_IMAGE_BYTES) {
    throw new Error("imageBytes exceeds 10MB limit");
  }

  const vimgAad = buildAAD("vimg", userId, aadId);
  const vthumbAad = buildAAD("vthumb", userId, aadId);
  const vmetaAad = buildAAD("vmeta", userId, aadId);

  const imageCt = await _encryptBlob(client, image, vimgAad);

  let thumbCt = null;
  if (thumbBytes) {
    thumbCt = await _encryptBlob(client, _toUint8(thumbBytes), vthumbAad);
  }

  const metaJson = JSON.stringify({ v: 1, ...meta });
  const metaEnc = await client.encrypt(
    new TextEncoder().encode(metaJson), vmetaAad,
  );

  const fileHashPlain = await sha256Hex(image);

  return {
    imageCt,
    thumbCt,
    metaBlob: metaEnc.ciphertext,
    metaIv: metaEnc.iv,
    fileHashPlain,
  };
}


/**
 * Step 3: 暗号文の実体を multipart で PUT する。
 *
 * @returns {Promise<Object>} サーバ JSON レスポンス
 */
export async function putVoucher({
  voucherId, imageCt, thumbCt, metaBlob, metaIv, fileHashPlain,
  fetchImpl, csrf,
}) {
  const f = fetchImpl ?? globalThis.fetch;
  const form = new FormData();
  form.append(
    "image_ct",
    new Blob([imageCt], { type: "application/octet-stream" }),
    "image.bin",
  );
  if (thumbCt) {
    form.append(
      "thumb_ct",
      new Blob([thumbCt], { type: "application/octet-stream" }),
      "thumb.bin",
    );
  }
  form.append("meta_blob", b64encode(metaBlob));
  form.append("meta_iv", b64encode(metaIv));
  form.append("file_hash_plain", fileHashPlain);

  // Content-Type は FormData の boundary をブラウザ/fetch に決めさせるため
  // 明示しない。
  const r = await f(`/api/v1/vouchers/${voucherId}`, {
    method: "PUT",
    credentials: "include",
    headers: { "X-CSRFToken": csrf ?? _csrf() },
    body: form,
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`putVoucher: HTTP ${r.status} ${e.error || ""}`);
  }
  return await r.json();
}


/**
 * 高レベルオーケストレータ: File → init → サムネ生成 → 暗号化 → PUT。
 *
 * @param {Object} args
 * @param {Object} args.client                SharedCryptoClient
 * @param {number|bigint} args.userId
 * @param {File|Blob} args.file               ユーザー選択画像
 * @param {number|null} [args.journalEntryId]
 * @param {Function} [args.makeThumbnail]     (file) => Promise<Uint8Array|null>
 *   DOM canvas 依存のサムネ生成。省略時はサムネなし。
 * @param {Function} [args.fetchImpl]
 * @param {string} [args.csrf]
 * @returns {Promise<{voucherId: number, aadId: bigint, ok: boolean,
 *   file_hash_cipher: string}>} aadId は再復号 (AAD 再構築) 用に返す。
 */
export async function uploadEncryptedVoucher({
  client, userId, file, journalEntryId = null, makeThumbnail,
  fetchImpl, csrf,
}) {
  const { voucherId, aadId } = await initVoucher({
    journalEntryId, fetchImpl, csrf,
  });

  const imageBytes = new Uint8Array(await file.arrayBuffer());

  let thumbBytes = null;
  if (typeof makeThumbnail === "function") {
    thumbBytes = await makeThumbnail(file);
  }

  const meta = {
    original_filename: file.name ?? null,
    image_mime: file.type || null,
  };

  const parts = await encryptVoucher({
    client, userId, aadId, imageBytes, thumbBytes, meta,
  });

  const res = await putVoucher({
    voucherId, ...parts, fetchImpl, csrf,
  });

  return { voucherId, aadId, ...res };
}
