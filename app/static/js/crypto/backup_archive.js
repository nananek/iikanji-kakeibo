// Phase v5 BU-3: 暗号化バックアップアーカイブ (.ikbackup) の encode / decode。
//
// パスフレーズ Argon2id + AES-256-GCM。MK と独立した「災害時用の鍵」を
// 持てるようにするため、本アーカイブ鍵は MK 派生から完全に切り離している
// ([[project_v5_backup_restore]] の方針)。
//
// バイナリレイアウト (little-endian は使わない、すべて big-endian):
//
//   offset  size  field
//   ------  ----  -------------------------------------------
//   0       8     magic       = "IKBKP\0\0\0"
//   8       1     version     = 0x01
//   9       3     reserved    = 0x000000
//   12      4     argon2_memory_kib   (uint32 BE)
//   16      4     argon2_iterations   (uint32 BE)
//   20      4     argon2_parallelism  (uint32 BE)
//   24      16    salt        (Argon2id salt, ランダム)
//   40      12    iv          (AES-GCM IV, ランダム)
//   52      4     ciphertext_len_low32 (uint32 BE, 4 GiB 超は将来拡張)
//   56      4     ciphertext_len_high32 (uint32 BE, 現状常に 0)
//   60      ...   ciphertext + 16B GCM tag (Web Crypto API は tag 末尾結合)
//
// ヘッダ長 = 60 bytes 固定。
//
// AAD: magic + version + reserved + argon2_* + salt = 先頭 40 bytes。
// IV を AAD に含めないのは AES-GCM の慣例 (IV はヘッダ平文として配るだけ)。
//
// 復号失敗 (パスフレーズ違い / 改ざん / フォーマット不一致) は throw する。

import { ARGON2ID_DEFAULTS, deriveKeyFromPassphrase } from "./argon2.js";


const MAGIC = new Uint8Array([0x49, 0x4B, 0x42, 0x4B, 0x50, 0x00, 0x00, 0x00]);  // "IKBKP\0\0\0"
const VERSION = 0x01;
const HEADER_LEN = 60;
const SALT_LEN = 16;
const IV_LEN = 12;


function _u32BE(view, offset, value) {
  view.setUint32(offset, value >>> 0, false);
}


function _readU32BE(view, offset) {
  return view.getUint32(offset, false);
}


function _bytesEqual(a, b) {
  if (a.byteLength !== b.byteLength) return false;
  for (let i = 0; i < a.byteLength; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}


function _buildHeader({ salt, iv, ciphertextLen, params }) {
  const buf = new Uint8Array(HEADER_LEN);
  buf.set(MAGIC, 0);
  buf[8] = VERSION;
  // reserved (3 bytes) は 0 のまま
  const view = new DataView(buf.buffer);
  _u32BE(view, 12, params.memorySize);
  _u32BE(view, 16, params.iterations);
  _u32BE(view, 20, params.parallelism);
  buf.set(salt, 24);
  buf.set(iv, 40);
  // ciphertext_len は 64 bit に分割 (現状 high32 = 0、4 GiB 未満想定)
  _u32BE(view, 52, ciphertextLen & 0xffffffff);
  _u32BE(view, 56, Math.floor(ciphertextLen / 0x100000000));
  return buf;
}


function _parseHeader(buf) {
  if (!(buf instanceof Uint8Array)) {
    throw new TypeError("archive must be a Uint8Array");
  }
  if (buf.byteLength < HEADER_LEN) {
    throw new Error(`archive too short: ${buf.byteLength} < ${HEADER_LEN}`);
  }
  if (!_bytesEqual(buf.subarray(0, 8), MAGIC)) {
    throw new Error("invalid magic (not a .ikbackup file)");
  }
  if (buf[8] !== VERSION) {
    throw new Error(`unsupported version: 0x${buf[8].toString(16)}`);
  }
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const memorySize = _readU32BE(view, 12);
  const iterations = _readU32BE(view, 16);
  const parallelism = _readU32BE(view, 20);
  const salt = buf.slice(24, 24 + SALT_LEN);
  const iv = buf.slice(40, 40 + IV_LEN);
  const lenLo = _readU32BE(view, 52);
  const lenHi = _readU32BE(view, 56);
  const ciphertextLen = lenHi * 0x100000000 + lenLo;
  if (buf.byteLength !== HEADER_LEN + ciphertextLen) {
    throw new Error(
      `archive length mismatch: header says ${HEADER_LEN + ciphertextLen}, ` +
      `got ${buf.byteLength}`,
    );
  }
  return {
    params: { memorySize, iterations, parallelism, hashLength: 32 },
    salt, iv, ciphertextLen,
    headerBytes: buf.subarray(0, HEADER_LEN),
    aad: buf.subarray(0, 40),  // magic+version+reserved+argon2_*+salt
    ciphertext: buf.subarray(HEADER_LEN),
  };
}


async function _importAesKey(rawKey) {
  return crypto.subtle.importKey(
    "raw", rawKey, { name: "AES-GCM", length: 256 }, false,
    ["encrypt", "decrypt"],
  );
}


/**
 * パスフレーズで plaintext を AES-256-GCM 暗号化し、`.ikbackup` バイナリを返す。
 *
 * @param {Uint8Array} plaintext   暗号化対象 (例: JSON.stringify を UTF-8 化したもの)
 * @param {string} passphrase      ユーザー入力パスフレーズ
 * @param {Object} [opts]
 * @param {Object} [opts.argon2Impl]  Argon2id 実装 (テスト DI 用)
 * @param {Object} [opts.argon2Params]  ARGON2ID_DEFAULTS を上書き
 * @returns {Promise<Uint8Array>}  完成した .ikbackup バイナリ
 */
export async function encryptBackupArchive(plaintext, passphrase, opts = {}) {
  if (!(plaintext instanceof Uint8Array)) {
    throw new TypeError("plaintext must be a Uint8Array");
  }
  if (typeof passphrase !== "string" || passphrase.length === 0) {
    throw new TypeError("passphrase must be non-empty string");
  }
  const params = { ...ARGON2ID_DEFAULTS, ...(opts.argon2Params ?? {}) };

  const salt = crypto.getRandomValues(new Uint8Array(SALT_LEN));
  const iv = crypto.getRandomValues(new Uint8Array(IV_LEN));

  // ヘッダの ciphertext_len は GCM tag 込みなので暗号化後に確定
  // 一旦 ダミー値で組み立てて AAD を作り、後でヘッダ全体を再構築する
  const aad = new Uint8Array(40);
  aad.set(MAGIC, 0);
  aad[8] = VERSION;
  const aadView = new DataView(aad.buffer);
  _u32BE(aadView, 12, params.memorySize);
  _u32BE(aadView, 16, params.iterations);
  _u32BE(aadView, 20, params.parallelism);
  aad.set(salt, 24);

  const derived = await deriveKeyFromPassphrase(
    passphrase, salt,
    { impl: opts.argon2Impl, params: { ...params, hashLength: 32 } },
  );
  try {
    const key = await _importAesKey(derived);
    const ciphertext = new Uint8Array(
      await crypto.subtle.encrypt(
        { name: "AES-GCM", iv, additionalData: aad },
        key, plaintext,
      ),
    );
    const header = _buildHeader({
      salt, iv, ciphertextLen: ciphertext.byteLength, params,
    });
    const out = new Uint8Array(HEADER_LEN + ciphertext.byteLength);
    out.set(header, 0);
    out.set(ciphertext, HEADER_LEN);
    return out;
  } finally {
    derived.fill(0);
  }
}


/**
 * `.ikbackup` バイナリをパスフレーズで復号して平文 Uint8Array を返す。
 *
 * @param {Uint8Array} archive    `.ikbackup` バイト列
 * @param {string} passphrase     入力パスフレーズ
 * @param {Object} [opts]
 * @param {Object} [opts.argon2Impl]  Argon2id 実装 (テスト DI 用)
 * @returns {Promise<Uint8Array>}  復号された平文
 */
export async function decryptBackupArchive(archive, passphrase, opts = {}) {
  if (typeof passphrase !== "string" || passphrase.length === 0) {
    throw new TypeError("passphrase must be non-empty string");
  }
  const parsed = _parseHeader(archive);
  const derived = await deriveKeyFromPassphrase(
    passphrase, parsed.salt,
    { impl: opts.argon2Impl, params: { ...parsed.params, hashLength: 32 } },
  );
  try {
    const key = await _importAesKey(derived);
    const plaintextBuf = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: parsed.iv, additionalData: parsed.aad },
      key, parsed.ciphertext,
    );
    return new Uint8Array(plaintextBuf);
  } finally {
    derived.fill(0);
  }
}


export const _internals = { MAGIC, VERSION, HEADER_LEN };
