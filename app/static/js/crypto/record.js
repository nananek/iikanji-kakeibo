// Phase E3: 仕訳 / 仕訳明細 / 医療費の record-level 暗号化 helper。
//
// 1 レコード = 1 JSON 暗号文 (JSON-then-encrypt) パターン。AAD には
// テーブル種別 + user_id + 識別 ID を big-endian で連結し、サーバが BLOB を
// 別ユーザー / 別行にすり替えても復号失敗で検出する。
//
// 設計書 §12.2 (AAD フォーマット) 参照。


// --- AAD 構築 ---


/** uint64 を 8B big-endian Uint8Array に変換。 */
export function uint64BE(n) {
  // Number は 53bit までしか安全ではないので BigInt 経由で 64bit 化
  const big = typeof n === "bigint" ? n : BigInt(n);
  if (big < 0n || big > 0xFFFF_FFFF_FFFF_FFFFn) {
    throw new RangeError(`uint64BE: out of range: ${n}`);
  }
  const out = new Uint8Array(8);
  const view = new DataView(out.buffer);
  // setBigUint64 は ES2020 / すべての主要ブラウザで対応
  view.setBigUint64(0, big, /* littleEndian */ false);
  return out;
}


function _concat(...arrays) {
  let len = 0;
  for (const a of arrays) len += a.byteLength;
  const out = new Uint8Array(len);
  let offset = 0;
  for (const a of arrays) {
    out.set(a, offset);
    offset += a.byteLength;
  }
  return out;
}


const TEXT_ENC = new TextEncoder();
const NUL = TEXT_ENC.encode("\0");


/**
 * AAD バイト列を構築。
 *
 * @param {"je"|"jel"|"me"|"bcb"} tableType  テーブル種別プレフィックス
 *   - "je"  = journal_entries
 *   - "jel" = journal_entry_lines
 *   - "me"  = medical_expenses
 *   - "bcb" = balance_cache_blobs (E3-E で導入予定)
 * @param {number|bigint} userId
 * @param {Array<number|bigint>} ids  識別 ID 列 (entry_id / line_id 等)
 * @returns {Uint8Array}
 */
export function buildAAD(tableType, userId, ...ids) {
  const ALLOWED = ["je", "jel", "me", "bcb"];
  if (!ALLOWED.includes(tableType)) {
    throw new Error(`buildAAD: unsupported tableType: ${tableType}`);
  }
  const parts = [TEXT_ENC.encode(tableType), NUL, uint64BE(userId)];
  for (const id of ids) {
    parts.push(NUL, uint64BE(id));
  }
  return _concat(...parts);
}


// --- encrypt / decrypt ---


/**
 * record (plain object) を JSON 化 → MK で AES-GCM 暗号化。
 *
 * @param {Object} client            SharedCryptoClient
 * @param {Object} record            シリアライズ対象の plain object
 *   - 内部に {v: 1, ...} を含めること推奨 (将来のスキーマ進化)
 * @param {Uint8Array} aad
 * @returns {Promise<{blob: Uint8Array, iv: Uint8Array}>}
 *   blob = ciphertext + 16B GCM tag, iv = 12B random
 */
export async function encryptRecord(client, record, aad) {
  if (!client || typeof client.encrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (!aad || !(aad instanceof Uint8Array)) {
    throw new Error("aad must be a Uint8Array");
  }
  const json = JSON.stringify(record);
  const plaintext = TEXT_ENC.encode(json);
  const res = await client.encrypt(plaintext, aad);
  return { blob: res.ciphertext, iv: res.iv };
}


/**
 * blob + iv + aad を復号 → JSON parse して record を返す。
 *
 * @param {Object} client
 * @param {Uint8Array} blob
 * @param {Uint8Array} iv
 * @param {Uint8Array} aad
 * @returns {Promise<Object>}
 *   AAD すり替えで GCM tag 検証に失敗 → SharedCryptoClient が throw する。
 */
export async function decryptRecord(client, blob, iv, aad) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const res = await client.decrypt(blob, iv, aad);
  const json = new TextDecoder().decode(res.plaintext);
  try { res.plaintext.fill(0); } catch (_e) { /* ignore */ }
  return JSON.parse(json);
}
