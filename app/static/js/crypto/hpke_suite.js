// Vendored HPKE (RFC 9180 base mode) のラッパー (E5 #112 / 設計書 §14.3)。
//
// suite = DHKEM-X25519-HKDF-SHA256 / HKDF-SHA256 / AES-256-GCM。
// - seal (送信側): 相手の公開鍵 (raw 32B) 宛に暗号化。秘密鍵不要・ephemeral 鍵は
//   ライブラリ内で生成され送付後破棄される (フォワードセクレシー)。
// - open (受信側): 自分の X25519 秘密鍵 (raw scalar 32B) で復号。
//
// 鍵フォーマット注意:
//   keypair.js は秘密鍵を WebCrypto の **pkcs8 (48B)** で保管するが、hpke-js は
//   WebCrypto CryptoKey を受け付けず raw scalar (32B) を要求する。pkcs8 の先頭
//   16B は固定 DER ヘッダなので末尾 32B が scalar。`pkcs8ToRawScalar()` で変換する。
//
// vendor は相対 import (ブラウザ main / module worker / Node テストの全環境で解決可)。

let _suitePromise = null;

async function _suite() {
  if (_suitePromise === null) {
    _suitePromise = import("../vendor/hpke-1.8.0.esm.min.js").then(
      (m) =>
        new m.CipherSuite({
          kem: new m.DhkemX25519HkdfSha256(),
          kdf: new m.HkdfSha256(),
          aead: new m.Aes256Gcm(),
        }),
    );
  }
  return _suitePromise;
}

/** Uint8Array view から、その範囲だけを指す ArrayBuffer を取り出す。 */
function _ab(u8) {
  return u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
}

/**
 * 相手の公開鍵 (raw 32B) 宛に plaintext を seal する。
 * @param {Uint8Array} recipientPublicKeyRaw  X25519 公開鍵 (raw 32B)
 * @param {Uint8Array} plaintext
 * @param {Uint8Array} aad
 * @returns {Promise<{enc: Uint8Array, ciphertext: Uint8Array}>}
 *   enc = HPKE encapsulated key (ephemeral 公開鍵, 32B)
 */
export async function hpkeSeal(recipientPublicKeyRaw, plaintext, aad) {
  const suite = await _suite();
  const pk = await suite.kem.deserializePublicKey(_ab(recipientPublicKeyRaw));
  const sender = await suite.createSenderContext({ recipientPublicKey: pk });
  const ciphertext = new Uint8Array(await sender.seal(plaintext, aad));
  const enc = new Uint8Array(sender.enc);
  return { enc, ciphertext };
}

/**
 * 自分の X25519 秘密鍵 (raw scalar 32B) で open する。
 * @param {Uint8Array} rawPriv32  X25519 秘密鍵 scalar (32B)
 * @param {Uint8Array} enc        HPKE encapsulated key (32B)
 * @param {Uint8Array} ciphertext
 * @param {Uint8Array} aad
 * @returns {Promise<Uint8Array>} 平文。AAD/鍵/暗号文の不一致は AEAD タグ検証で throw。
 */
export async function hpkeOpenWithRawPriv(rawPriv32, enc, ciphertext, aad) {
  const suite = await _suite();
  const sk = await suite.kem.deserializePrivateKey(_ab(rawPriv32));
  const recipient = await suite.createRecipientContext({
    recipientKey: sk,
    enc: _ab(enc),
  });
  return new Uint8Array(await recipient.open(_ab(ciphertext), aad));
}

/**
 * X25519 pkcs8 秘密鍵 (48B) → raw scalar (32B)。先頭 16B は固定 DER ヘッダ。
 * 呼び出し側は使用後に戻り値を `.fill(0)` でゼロ埋めすること。
 */
export function pkcs8ToRawScalar(pkcs8) {
  if (!(pkcs8 instanceof Uint8Array) || pkcs8.byteLength !== 48) {
    throw new Error("X25519 pkcs8 must be Uint8Array of 48 bytes");
  }
  return pkcs8.slice(16);
}
