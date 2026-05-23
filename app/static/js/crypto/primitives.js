// E2EE 暗号プリミティブ (純粋関数、Worker / Node 両環境で実行可)。
//
// Worker からは worker.js が import し、Node テストからも import 可能。
// 設計書 §3 / §10 の AES-GCM パターンに準拠。

export function isUint8(v) {
  return v instanceof Uint8Array;
}

export function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/** raw bytes (32B) を AES-GCM CryptoKey に import。raw は呼び出し側でゼロ埋め推奨。 */
export async function importAesKey(rawBytes, usages) {
  if (!isUint8(rawBytes) || rawBytes.byteLength !== 32) {
    throw new Error("key must be Uint8Array of 32 bytes");
  }
  return crypto.subtle.importKey(
    "raw", rawBytes, { name: "AES-GCM" }, false, usages,
  );
}

/** AES-256-GCM 暗号化。aad は任意 (Uint8Array)。戻り値 { iv (12B), ciphertext (tag 込) }。 */
export async function aesGcmEncrypt(key, plaintext, aad) {
  if (!isUint8(plaintext)) throw new Error("plaintext must be Uint8Array");
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const params = { name: "AES-GCM", iv };
  if (aad !== undefined) {
    if (!isUint8(aad)) throw new Error("aad must be Uint8Array");
    params.additionalData = aad;
  }
  const ct = await crypto.subtle.encrypt(params, key, plaintext);
  return { iv, ciphertext: new Uint8Array(ct) };
}

/** AES-256-GCM 復号。iv は 12B、aad は暗号化時と同一が必須。 */
export async function aesGcmDecrypt(key, ciphertext, iv, aad) {
  if (!isUint8(ciphertext)) throw new Error("ciphertext must be Uint8Array");
  if (!isUint8(iv) || iv.byteLength !== 12) {
    throw new Error("iv must be Uint8Array of 12 bytes");
  }
  const params = { name: "AES-GCM", iv };
  if (aad !== undefined) {
    if (!isUint8(aad)) throw new Error("aad must be Uint8Array");
    params.additionalData = aad;
  }
  const pt = await crypto.subtle.decrypt(params, key, ciphertext);
  return new Uint8Array(pt);
}

/** rawMK (32B) を rawWrappingKey (32B) で AES-GCM 暗号化。{ iv, wrapped } を返す。 */
export async function wrapMasterKey(rawMk, rawWrappingKey) {
  if (!isUint8(rawMk) || rawMk.byteLength !== 32) {
    throw new Error("rawMk must be Uint8Array of 32 bytes");
  }
  if (!isUint8(rawWrappingKey) || rawWrappingKey.byteLength !== 32) {
    throw new Error("rawWrappingKey must be Uint8Array of 32 bytes");
  }
  const wrappingKey = await importAesKey(rawWrappingKey, ["encrypt"]);
  return aesGcmEncrypt(wrappingKey, rawMk);
}

/** wrapped を rawWrappingKey (32B) で AES-GCM 復号 → rawMK (32B) を返す。 */
export async function unwrapMasterKey(wrapped, iv, rawWrappingKey) {
  if (!isUint8(rawWrappingKey) || rawWrappingKey.byteLength !== 32) {
    throw new Error("rawWrappingKey must be Uint8Array of 32 bytes");
  }
  const wrappingKey = await importAesKey(rawWrappingKey, ["decrypt"]);
  return aesGcmDecrypt(wrappingKey, wrapped, iv);
}
