// E5 (#111): AI 下書き画像のクライアント完結 E2EE upload。
//
// E4 証憑の 2 段階 upload (init/PUT) を AI 下書き (ai_drafts) に適用する:
//   1. initAiDraft()        → POST /api/v1/ai/uploads/init で draft_id 採番 + aad_id 受領
//   2. encryptVoucher()     → aad_id を AAD に束縛して画像/サムネ/メタを暗号化
//   3. putAiDraft()         → PUT /api/v1/ai/uploads/<id> で暗号文の実体を upload
//
// 暗号化ロジックは voucher_upload.encryptVoucher をそのまま再利用する。AAD
// ドメイン (vimg/vthumb/vmeta) が証憑と同一なため、下書き → 証憑移行
// (create_voucher_from_draft) 時に**再暗号化なし**で AAD を維持できる
// (サーバ側で draft の aad_id を Voucher.aad_id へ引き継ぐ)。
//
// aad_id は 63bit のためサーバは文字列で返し、クライアントは BigInt として
// AAD に渡す (voucher_upload と同様)。DOM (canvas) 依存のサムネイル生成は
// 呼び出し側が `makeThumbnail` として注入する (Node 単体テスト可能にするため)。
//
// 設計書 §12.6 / §13.6 参照。

import { encryptVoucher } from "./voucher_upload.js";
import { b64encode } from "./b64.js";


function _csrf() {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


/**
 * Step 1: 空 AIDraft を作成して draft_id を採番する。
 *
 * @param {Object} args
 * @param {string|null} [args.comment]  メモ (任意、サーバ側で 500 文字に切詰め)
 * @param {Function} [args.fetchImpl]   テスト DI
 * @param {string} [args.csrf]          CSRF トークン (省略時は meta tag)
 * @returns {Promise<{draftId: number, aadId: bigint}>}
 *   draftId は URL/storage 用 + 解析結果保存先、aadId は AAD 束縛用 (BigInt)。
 */
export async function initAiDraft({ comment = null, fetchImpl, csrf } = {}) {
  const f = fetchImpl ?? globalThis.fetch;
  const r = await f("/api/v1/ai/uploads/init", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrf ?? _csrf(),
    },
    body: JSON.stringify({ comment }),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`initAiDraft: HTTP ${r.status} ${e.error || ""}`);
  }
  const data = await r.json();
  // aad_id は文字列で返る (63bit, JS Number 精度対策) → BigInt にパース。
  if (data.aad_id == null) {
    throw new Error("initAiDraft: サーバが aad_id を返しませんでした。");
  }
  return { draftId: data.draft_id, aadId: BigInt(data.aad_id) };
}


/**
 * Step 3: 暗号文の実体を multipart で PUT する。
 *
 * @returns {Promise<Object>} サーバ JSON レスポンス
 */
export async function putAiDraft({
  draftId, imageCt, thumbCt, metaBlob, metaIv, fileHashPlain,
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

  const id = encodeURIComponent(String(draftId));
  // Content-Type は FormData の boundary をブラウザ/fetch に決めさせる。
  const r = await f(`/api/v1/ai/uploads/${id}`, {
    method: "PUT",
    credentials: "include",
    headers: { "X-CSRFToken": csrf ?? _csrf() },
    body: form,
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`putAiDraft: HTTP ${r.status} ${e.error || ""}`);
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
 * @param {string|null} [args.comment]
 * @param {Function} [args.makeThumbnail]     (file) => Promise<Uint8Array|null>
 *   DOM canvas 依存のサムネ生成。省略時はサムネなし。
 * @param {Function} [args.fetchImpl]
 * @param {string} [args.csrf]
 * @returns {Promise<{draftId: number, aadId: bigint, ok: boolean,
 *   status: string, file_hash_cipher: string}>} aadId は再復号用に返す。
 */
export async function uploadEncryptedDraft({
  client, userId, file, comment = null, makeThumbnail,
  fetchImpl, csrf,
}) {
  const { draftId, aadId } = await initAiDraft({ comment, fetchImpl, csrf });

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

  const res = await putAiDraft({ draftId, ...parts, fetchImpl, csrf });

  return { draftId, aadId, ...res };
}
