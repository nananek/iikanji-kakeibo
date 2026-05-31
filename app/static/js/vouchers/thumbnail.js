// E4 (#111): クライアントサイドサムネイル生成 (canvas, 長辺 200px)。
//
// サーバ Pillow 生成 (E4 後に廃止) の代替。原画像はサーバから見えないため、
// クライアントが暗号化前にサムネイルを生成する (設計書 §13.3)。
//
// DOM (createImageBitmap / canvas / toBlob) 依存のため crypto/ ではなく
// vouchers/ に置く (Node の crypto カバレッジゲート対象外、Playwright E2E で
// 検証する)。voucher_upload.uploadEncryptedVoucher の makeThumbnail として注入。

export const THUMB_MAX = 200;


/**
 * File/Blob を長辺 maxSize の JPEG サムネイルに縮小し、バイト列を返す。
 *
 * @param {File|Blob} file
 * @param {number} [maxSize=200]   長辺の最大ピクセル
 * @param {number} [quality=0.85]  JPEG 品質 (0..1)
 * @returns {Promise<Uint8Array>}  JPEG サムネイルのバイト列
 */
export async function makeThumbnail(file, maxSize = THUMB_MAX, quality = 0.85) {
  const bitmap = await createImageBitmap(file);
  try {
    const longSide = Math.max(bitmap.width, bitmap.height) || 1;
    const scale = Math.min(1, maxSize / longSide);
    const w = Math.max(1, Math.round(bitmap.width * scale));
    const h = Math.max(1, Math.round(bitmap.height * scale));

    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bitmap, 0, 0, w, h);

    const blob = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", quality),
    );
    if (!blob) throw new Error("makeThumbnail: canvas.toBlob returned null");
    return new Uint8Array(await blob.arrayBuffer());
  } finally {
    if (typeof bitmap.close === "function") bitmap.close();
  }
}
