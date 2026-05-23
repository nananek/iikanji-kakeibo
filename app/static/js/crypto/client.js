// E2EE Master Key 管理クライアント (メインスレッド ↔ Worker のラッパー)。
// 設計書 §5 / §10 参照。
//
// 使用例:
//   const client = new CryptoClient("/static/js/crypto/worker.js");
//   await client.generateKey();
//   const { iv, ciphertext } = await client.encrypt(utf8("hello"));
//   const plaintext = await client.decrypt(ciphertext, iv);
//   console.log(fromUtf8(plaintext));  // "hello"

export class CryptoClient {
  constructor(workerUrl) {
    // #worker (private field) で外部からの直接 postMessage を防ぐ
    this.worker = new Worker(workerUrl, { type: "module" });
    this.nextId = 1;
    this.pending = new Map();
    this.worker.onmessage = (ev) => {
      const { id, ok, error, ...rest } = ev.data || {};
      const slot = this.pending.get(id);
      if (!slot) return;
      this.pending.delete(id);
      if (ok) slot.resolve(rest);
      else slot.reject(new Error(error || "worker error"));
    };
    const failAll = (label) => (ev) => {
      const err = new Error(
        `${label}: ${(ev && (ev.message || ev.type)) || "unknown"}`,
      );
      for (const { reject } of this.pending.values()) reject(err);
      this.pending.clear();
    };
    this.worker.onerror = failAll("worker crashed");
    this.worker.onmessageerror = failAll("worker message error");
  }

  _send(payload) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage({ id, ...payload });
    });
  }

  /** MK を 32B 乱数で生成。raw はメインスレッドに渡らない。 */
  generateKey() {
    return this._send({ type: "generateKey" });
  }

  /**
   * 既存の rawKey (Uint8Array 32B) で MK を設定。
   * rawKey は呼び出し後に Worker 内でゼロ埋めされる。
   */
  setKey(rawKey) {
    return this._send({ type: "setKey", rawKey });
  }

  clearKey() {
    return this._send({ type: "clearKey" });
  }

  /** plaintext を AES-GCM で暗号化。aad は省略可。 */
  encrypt(plaintext, aad) {
    return this._send({ type: "encrypt", plaintext, aad });
  }

  /** ciphertext + iv (+ aad) を AES-GCM で復号。 */
  decrypt(ciphertext, iv, aad) {
    return this._send({ type: "decrypt", ciphertext, iv, aad });
  }

  /**
   * MK を derivedKey (Uint8Array 32B) で AES-GCM 暗号化。
   * derivedKey は呼び出し後に Worker 内でゼロ埋めされる。
   * 戻り値: { iv, wrapped }
   */
  wrap(derivedKey) {
    return this._send({ type: "wrap", derivedKey });
  }

  /**
   * derivedKey で wrap blob を復号 → MK に設定。
   * derivedKey は呼び出し後に Worker 内でゼロ埋めされる。
   * 戻り値: { keyBits }
   */
  unwrap(derivedKey, wrapped, iv) {
    return this._send({ type: "unwrap", derivedKey, wrapped, iv });
  }

  terminate() {
    this.worker.terminate();
    for (const { reject } of this.pending.values()) {
      reject(new Error("worker terminated"));
    }
    this.pending.clear();
  }
}

export function utf8(s) {
  return new TextEncoder().encode(s);
}

export function fromUtf8(bytes) {
  return new TextDecoder().decode(bytes);
}
