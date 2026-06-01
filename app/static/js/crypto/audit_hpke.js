// 監査連携の HPKE seal + AAD + snapshot_hash (メインスレッド, E5 #112 / §14.3)。
//
// seal は秘密鍵不要 (相手の公開鍵 + ephemeral 鍵) なのでメインスレッドで実行する。
// open (受信復号) は秘密鍵が要るため SharedWorker 内で行う (shared-client.hpkeOpen)。
//
// AAD 設計 (§14.3 からの調整):
//   PR-C の POST は 1 段階で seal 時に audit_package_id が未確定のため、AAD は
//   クライアントが seal 前に知る値で束縛する。
//   - AuditPackage : "ap" + uint64BE(audit_grant_id) + uint32BE(round_id)  = 14B
//   - AuditResponse: "ar" + uint64BE(audit_package_id)                      = 10B

import { uint64BE } from "./record.js";
import { hpkeSeal } from "./hpke_suite.js";

const TEXT_ENC = new TextEncoder();

/** uint32 を 4B big-endian Uint8Array に変換。 */
export function uint32BE(n) {
  if (!Number.isInteger(n) || n < 0 || n > 0xffff_ffff) {
    throw new RangeError(`uint32BE: out of range: ${n}`);
  }
  const out = new Uint8Array(4);
  new DataView(out.buffer).setUint32(0, n, /* littleEndian */ false);
  return out;
}

function _concat(...arrays) {
  let len = 0;
  for (const a of arrays) len += a.byteLength;
  const out = new Uint8Array(len);
  let off = 0;
  for (const a of arrays) {
    out.set(a, off);
    off += a.byteLength;
  }
  return out;
}

/** AuditPackage 用 AAD: "ap" + uint64BE(grantId) + uint32BE(roundId) (14B)。 */
export function packageAAD(grantId, roundId) {
  return _concat(TEXT_ENC.encode("ap"), uint64BE(grantId), uint32BE(roundId));
}

/** AuditResponse 用 AAD: "ar" + uint64BE(packageId) (10B)。 */
export function responseAAD(packageId) {
  return _concat(TEXT_ENC.encode("ar"), uint64BE(packageId));
}

/** SHA-256(plaintext) を 32B で返す (§14.3 の snapshot_hash)。 */
export async function snapshotHash(plaintext) {
  if (!(plaintext instanceof Uint8Array)) {
    throw new TypeError("snapshotHash: plaintext must be a Uint8Array");
  }
  const buf = plaintext.buffer.slice(
    plaintext.byteOffset,
    plaintext.byteOffset + plaintext.byteLength,
  );
  return new Uint8Array(await crypto.subtle.digest("SHA-256", buf));
}

/**
 * owner → auditor: スナップショット平文を相手の公開鍵で seal する。
 * @returns {Promise<{ephemeralPubkey: Uint8Array, ciphertext: Uint8Array, snapshotHash: Uint8Array}>}
 *   そのまま POST /api/v1/audit-packages の ephemeral_pubkey / ciphertext / snapshot_hash に渡す。
 */
export async function sealAuditPackage(recipientPublicKeyRaw, plaintext, grantId, roundId) {
  const aad = packageAAD(grantId, roundId);
  const { enc, ciphertext } = await hpkeSeal(recipientPublicKeyRaw, plaintext, aad);
  const hash = await snapshotHash(plaintext);
  return { ephemeralPubkey: enc, ciphertext, snapshotHash: hash };
}

/**
 * auditor → owner: 修正案 / 差戻し平文を相手の公開鍵で seal する。
 * @returns {Promise<{ephemeralPubkey: Uint8Array, ciphertext: Uint8Array}>}
 */
export async function sealAuditResponse(recipientPublicKeyRaw, plaintext, packageId) {
  const aad = responseAAD(packageId);
  const { enc, ciphertext } = await hpkeSeal(recipientPublicKeyRaw, plaintext, aad);
  return { ephemeralPubkey: enc, ciphertext };
}
