// メインスレッド側ラッパー。Worker への postMessage を Promise 化し、id で対応付ける。
// Master Key はこのスレッドでは扱わず、generateKey/setKey の rawKey 受け渡しのみで完結させる。

export class CryptoClient {
  constructor(workerUrl) {
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
      const err = new Error(`${label}: ${ev && (ev.message || ev.type) || "unknown"}`);
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

  generateKey() {
    return this._send({ type: "generateKey" });
  }

  setKey(rawKey) {
    return this._send({ type: "setKey", rawKey });
  }

  clearKey() {
    return this._send({ type: "clearKey" });
  }

  encrypt(plaintext, aad) {
    return this._send({ type: "encrypt", plaintext, aad });
  }

  decrypt(ciphertext, iv, aad) {
    return this._send({ type: "decrypt", ciphertext, iv, aad });
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
