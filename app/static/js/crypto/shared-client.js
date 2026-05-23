// SharedWorker 版 CryptoClient (設計書 §10.7)。
//
// Dedicated Worker 版 (client.js の CryptoClient) と API は同じだが、
// `new SharedWorker(url).port` 経由で複数タブが同一 Worker を共有する。
// MK 状態変化は `client.on("mkChanged" | "mkCleared", handler)` で受信可能。
//
// 鍵素材 (rawKey / derivedKey) は Transferable として渡す方針も同じ。
// メインスレッドに raw bytes の残骸を残さない。
//
// 使用例:
//   const client = new SharedCryptoClient("/static/js/crypto/shared-worker.js");
//   client.on("mkCleared", () => showLockScreen());
//   await client.setKey(rawMk);  // 全タブで同期される
//   const { ciphertext, iv } = await client.encrypt(utf8("hello"));

export class SharedCryptoClient {
  #port;
  #nextId = 1;
  #pending = new Map();
  // event → handler のセット。複数ハンドラ対応。
  #listeners = new Map();
  // 最後に受信した状態イベント名。on() 登録時の race 解消用に保持
  // (constructor 直後に SharedWorker から初期状態 broadcast が届くため、
  //  on(...) を constructor の外で登録すると取りこぼす可能性がある)。
  #lastEvent = null;

  /**
   * @param {string} workerUrl  SharedWorker スクリプト URL
   * @param {string} name       SharedWorker 名 (同名なら同一インスタンスに接続)
   */
  constructor(workerUrl, name = "iikanji-mk") {
    const sw = new SharedWorker(workerUrl, { type: "module", name });
    this.#port = sw.port;
    this.#port.onmessage = (ev) => {
      const data = ev.data || {};
      // broadcast イベントは {event: "mkChanged"} 形式 (id を持たない)
      if (typeof data.event === "string") {
        this.#lastEvent = data.event;
        const set = this.#listeners.get(data.event);
        if (set) {
          for (const h of set) {
            try {
              h();
            } catch (_e) {
              // ハンドラ例外は他のハンドラに伝播させない
            }
          }
        }
        return;
      }
      const { id, ok, error, ...rest } = data;
      const slot = this.#pending.get(id);
      if (!slot) return;
      this.#pending.delete(id);
      if (ok) slot.resolve(rest);
      else slot.reject(new Error(error || "worker error"));
    };
    this.#port.onmessageerror = (ev) => {
      const err = new Error(`shared worker message error: ${ev?.type || "?"}`);
      for (const { reject } of this.#pending.values()) reject(err);
      this.#pending.clear();
    };
    this.#port.start();
  }

  /**
   * 状態変化イベント購読。返値は購読解除関数。
   * 利用可能イベント:
   *   "mkChanged" — MK が新規設定された (他タブでの setKey/unwrap も含む)
   *   "mkCleared" — MK が消去された (clearKey or 60 分 idle 自動ロック)
   *
   * race 解消: SharedWorker は接続直後に現在状態 (mkChanged or mkCleared) を
   * broadcast するため、コンストラクタの外で on() を遅延登録するパターン
   * では初期メッセージを取りこぼす恐れがある。本実装では最後に受信した
   * イベントを `#lastEvent` に保持し、on() 登録時に該当ハンドラを即時
   * 呼び出すことで取りこぼしを防ぐ。
   */
  on(eventName, handler) {
    if (typeof handler !== "function") throw new Error("handler must be function");
    let set = this.#listeners.get(eventName);
    if (!set) {
      set = new Set();
      this.#listeners.set(eventName, set);
    }
    set.add(handler);
    // 既に同じ event が届いている場合は即時 invoke (初期状態同期の race 解消)
    if (this.#lastEvent === eventName) {
      try {
        handler();
      } catch (_e) {
        // ハンドラ例外は伝播させない
      }
    }
    return () => set.delete(handler);
  }

  #send(payload, transferKeys = []) {
    const id = this.#nextId++;
    const transferables = [];
    for (const k of transferKeys) {
      const val = payload[k];
      if (val instanceof Uint8Array) transferables.push(val.buffer);
    }
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
      this.#port.postMessage({ id, ...payload }, transferables);
    });
  }

  /**
   * 新規 MK を生成 (32B 乱数)。
   *
   * **警告**: 既に MK が設定済みでも無警告で上書きする破壊的操作。
   * 既存暗号文 (UserAIConfig / 仕訳等) は復号不能になるため、初回設定 or
   * 意図的なローテーション以外では呼び出さないこと。呼び出し側は事前に
   * `status()` で `hasKey: false` を確認することを推奨。
   */
  generateKey() {
    return this.#send({ type: "generateKey" });
  }

  setKey(rawKey) {
    return this.#send({ type: "setKey", rawKey }, ["rawKey"]);
  }

  clearKey() {
    return this.#send({ type: "clearKey" });
  }

  encrypt(plaintext, aad) {
    return this.#send({ type: "encrypt", plaintext, aad });
  }

  decrypt(ciphertext, iv, aad) {
    return this.#send({ type: "decrypt", ciphertext, iv, aad });
  }

  wrap(derivedKey) {
    return this.#send({ type: "wrap", derivedKey }, ["derivedKey"]);
  }

  unwrap(derivedKey, wrapped, iv) {
    return this.#send(
      { type: "unwrap", derivedKey, wrapped, iv },
      ["derivedKey"],
    );
  }

  /** アクティビティ通知 (idle タイマーリセット)。IdleMonitor から呼ばれる。 */
  touch() {
    return this.#send({ type: "touch" });
  }

  /** デバッグ・UI 表示用の状態取得。 { hasKey, lastActivity, idleMs } */
  status() {
    return this.#send({ type: "status" });
  }

  close() {
    try {
      this.#port.close();
    } catch (_e) {
      // 既に閉じている場合は無視
    }
    for (const { reject } of this.#pending.values()) {
      reject(new Error("shared worker port closed"));
    }
    this.#pending.clear();
    this.#listeners.clear();
  }
}
