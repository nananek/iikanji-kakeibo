// E2EE Master Key 管理クライアント (メインスレッド ↔ Worker のラッパー)。
// 設計書 §5 / §10 参照。
//
// 重要: 鍵素材 (rawKey / derivedKey) は Transferable として Worker に渡す。
// 呼び出し後、渡した Uint8Array の .buffer は detached になり読めなくなる。
// メインスレッドに鍵素材の残骸を残さないため。
//
// 使用例:
//   const client = new CryptoClient("/static/js/crypto/worker.js");
//   await client.generateKey();
//   const { iv, ciphertext } = await client.encrypt(utf8("hello"));
//   const plaintext = await client.decrypt(ciphertext, iv);

export class CryptoClient {
  // private fields: 外部 (XSS 等) からの postMessage 偽装・Promise 汚染を防ぐ
  #worker;
  #nextId = 1;
  #pending = new Map();

  constructor(workerUrl) {
    this.#worker = new Worker(workerUrl, { type: "module" });
    this.#worker.onmessage = (ev) => {
      const { id, ok, error, ...rest } = ev.data || {};
      const slot = this.#pending.get(id);
      if (!slot) return;
      this.#pending.delete(id);
      if (ok) slot.resolve(rest);
      else slot.reject(new Error(error || "worker error"));
    };
    const failAll = (label) => (ev) => {
      const err = new Error(
        `${label}: ${(ev && (ev.message || ev.type)) || "unknown"}`,
      );
      for (const { reject } of this.#pending.values()) reject(err);
      this.#pending.clear();
    };
    this.#worker.onerror = failAll("worker crashed");
    this.#worker.onmessageerror = failAll("worker message error");
  }

  /**
   * Worker にメッセージ送信。鍵素材は Transferable として渡し、メインスレッド
   * 側の ArrayBuffer を detach する。呼び出し元は同じ Uint8Array を以降使えない。
   */
  #send(payload, transferKeys = []) {
    const id = this.#nextId++;
    const transferables = [];
    for (const k of transferKeys) {
      const val = payload[k];
      if (val instanceof Uint8Array) transferables.push(val.buffer);
    }
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
      this.#worker.postMessage({ id, ...payload }, transferables);
    });
  }

  /** MK を 32B 乱数で生成。raw はメインスレッドに渡らない。 */
  generateKey() {
    return this.#send({ type: "generateKey" });
  }

  /**
   * 既存の rawKey (Uint8Array 32B) で MK を設定。
   * **呼び出し後 rawKey.buffer は detached** されメインスレッドから読めなくなる。
   */
  setKey(rawKey) {
    return this.#send({ type: "setKey", rawKey }, ["rawKey"]);
  }

  clearKey() {
    return this.#send({ type: "clearKey" });
  }

  /** plaintext を AES-GCM で暗号化。aad は省略可。 */
  encrypt(plaintext, aad) {
    return this.#send({ type: "encrypt", plaintext, aad });
  }

  /** ciphertext + iv (+ aad) を AES-GCM で復号。 */
  decrypt(ciphertext, iv, aad) {
    return this.#send({ type: "decrypt", ciphertext, iv, aad });
  }

  /**
   * MK を derivedKey (Uint8Array 32B) で AES-GCM 暗号化。
   * **呼び出し後 derivedKey.buffer は detached** される。
   * 戻り値: { iv, wrapped }
   */
  wrap(derivedKey) {
    return this.#send({ type: "wrap", derivedKey }, ["derivedKey"]);
  }

  /**
   * derivedKey で wrap blob を復号 → MK に設定。
   * **呼び出し後 derivedKey.buffer は detached** される。
   * 戻り値: { keyBits }
   */
  unwrap(derivedKey, wrapped, iv) {
    return this.#send(
      { type: "unwrap", derivedKey, wrapped, iv },
      ["derivedKey"],
    );
  }

  terminate() {
    this.#worker.terminate();
    for (const { reject } of this.#pending.values()) {
      reject(new Error("worker terminated"));
    }
    this.#pending.clear();
  }
}

export function utf8(s) {
  return new TextEncoder().encode(s);
}

export function fromUtf8(bytes) {
  return new TextDecoder().decode(bytes);
}
