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

// #send タイムアウト (PR-F3b 申し送り対応)。SharedWorker がクラッシュ
// または応答不能になった場合、#pending に積まれた Promise が永久に
// resolve/reject されないと UI がハングする。30 秒で AbortSignal 相当の
// 動作で reject することで失敗を伝搬する。
//
// 通常時の処理は数 ms 〜 数百 ms (Argon2id 派生でも 1-2 秒) なので、
// 30 秒は余裕のあるデフォルト。設定可能。
const DEFAULT_TIMEOUT_MS = 30 * 1000;

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
  #timeoutMs;

  /**
   * @param {string} workerUrl  SharedWorker スクリプト URL
   * @param {Object|string} [opts]  name (string) または options object
   * @param {string} [opts.name]    SharedWorker 名 (同名なら同一インスタンスに接続)
   * @param {number} [opts.timeoutMs]  #send タイムアウト ms (デフォルト 30000)
   */
  constructor(workerUrl, opts = {}) {
    // 後方互換: string が渡されたら name として扱う
    const config = typeof opts === "string" ? { name: opts } : opts;
    const name = config.name ?? "iikanji-mk";
    this.#timeoutMs = config.timeoutMs ?? DEFAULT_TIMEOUT_MS;
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
      // タイムアウトハンドル: 期限超過で Worker からの応答を諦めて reject。
      // resolve/reject 時にクリアする。
      const timeoutId = setTimeout(() => {
        if (this.#pending.has(id)) {
          this.#pending.delete(id);
          reject(new Error(
            `shared worker request timed out after ${this.#timeoutMs}ms (type=${payload?.type})`,
          ));
        }
      }, this.#timeoutMs);
      const wrappedResolve = (val) => {
        clearTimeout(timeoutId);
        resolve(val);
      };
      const wrappedReject = (err) => {
        clearTimeout(timeoutId);
        reject(err);
      };
      this.#pending.set(id, { resolve: wrappedResolve, reject: wrappedReject });
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

  /**
   * E5 #112: HPKE 受信復号。自分の MK ラップ秘密鍵で audit パッケージ/レスポンスを
   * worker 内で復号する。平文秘密鍵はメインスレッドに出ない。
   * @param {Object} a
   * @param {Uint8Array} a.encryptedPrivateKey  MK でラップした X25519 秘密鍵 (pkcs8)
   * @param {Uint8Array} a.privIv                その AES-GCM IV
   * @param {Uint8Array} a.privAad               秘密鍵ラップ時の AAD (keypair.privateKeyAAD)
   * @param {Uint8Array} a.enc                   HPKE encapsulated key (ephemeral 公開鍵)
   * @param {Uint8Array} a.ciphertext            HPKE 暗号文
   * @param {Uint8Array} a.aad                   audit_hpke.packageAAD / responseAAD
   * @returns {Promise<{plaintext: Uint8Array}>}
   */
  hpkeOpen({ encryptedPrivateKey, privIv, privAad, enc, ciphertext, aad }) {
    return this.#send({
      type: "hpkeOpen",
      encryptedPrivateKey, privIv, privAad, enc, ciphertext, aad,
    });
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
