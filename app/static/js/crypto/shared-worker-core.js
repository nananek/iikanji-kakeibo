// SharedWorker 用 MK 状態機械 (純粋ロジック、Node テスト可能)。
// 設計書 §10.7 (SharedWorker による MK 永続化と idle 自動ロック)。
//
// 設計方針:
// - MK を Worker クロージャに保持し、メインスレッドに raw bytes を出さない
//   のは worker.js (Dedicated Worker) と同じ。SharedWorker 化により
//   ・同一 origin の全タブで MK 共有 (タブ間で再アンラップ不要)
//   ・タブが 1 つでも生きていればリロード跨ぎで MK 維持
//   ・どのタブもアクティブでない時間が 60 分続くと idle 自動ロック
// - timer (setInterval) や onconnect 等の Worker globals は呼び出し側に切り出し、
//   この core モジュールは「state + handle(msg) + checkIdle(now)」のみを提供。
//
// 並行性: worker.js と同じく、await 中の clearKey 割り込みに備えて wrap は
// rawMasterKey.slice() でスナップショットを取る。エラーパスでもゼロ埋め
// (try/finally) を全 case で徹底。

import {
  aesGcmDecrypt,
  aesGcmEncrypt,
  importAesKey,
  isPlainObject,
  isUint8,
  unwrapMasterKey,
  wrapMasterKey,
} from "./primitives.js";

// idle タイムアウト = 60 分 (設計書 §10.7)。
// ユーザー判断で 30 分から 60 分に拡大。短いと操作中の誤ロックで UX 低下、
// 長いと放置端末のリスク。Passkey 解除なら 1 タップで復帰するためロックが
// 結果としてパスフレーズ → Passkey 移行の動機にもなる。
export const IDLE_LIMIT_MS = 60 * 60 * 1000;

// idle 判定の実行間隔 (Worker setInterval で使う想定値)。
export const IDLE_CHECK_INTERVAL_MS = 60 * 1000;

/**
 * SharedWorker 内で MK を保持する状態機械。
 *
 * Worker から `state.handle(msg, now)` を呼び、その戻り値 `{ result, broadcast }`
 * を元にレスポンス送信 + 全ポート broadcast を行う。
 *
 * broadcast の値:
 *   "mkChanged"  — MK が新しく設定された (generateKey/setKey/unwrap)
 *   "mkCleared"  — MK が消去された (clearKey または idle 自動ロック)
 *   null         — broadcast 不要 (encrypt/decrypt/touch/status)
 */
export class MasterKeyState {
  constructor() {
    this.cryptoKey = null;       // CryptoKey (AES-GCM encrypt/decrypt)
    this.rawMasterKey = null;    // Uint8Array 32B (wrap 用、Worker 内のみ)
    this.lastActivity = 0;       // ms timestamp、touch/encrypt 等で更新
    this.hasKey = false;         // 状態通知用フラグ (rawMasterKey 公開を避けるため)
  }

  async _setRaw(rawBytes) {
    if (!isUint8(rawBytes) || rawBytes.byteLength !== 32) {
      throw new Error("master key must be Uint8Array of 32 bytes");
    }
    // 順序の意図 (importAesKey 失敗時にも整合性を保つ):
    //   1) await して新 cryptoKey を得る — ここで throw すれば全フィールド未変更で巻き戻る
    //   2) 以降は同期ブロック: cryptoKey 代入 → 旧 rawMasterKey ゼロ埋め →
    //      新 rawMasterKey 代入 → hasKey=true まで他タスク割り込み不可
    //      (JS は単一スレッド、await のない連続代入は他のメッセージハンドラから
    //       中間状態として観測されない)
    // 逆に「rawMasterKey を await 前に先行代入」は failure 時に
    // cryptoKey と rawMasterKey が不整合になるため不可。
    const newCryptoKey = await importAesKey(rawBytes, ["encrypt", "decrypt"]);
    this.cryptoKey = newCryptoKey;
    if (this.rawMasterKey) this.rawMasterKey.fill(0);
    this.rawMasterKey = new Uint8Array(rawBytes); // コピー保持
    this.hasKey = true;
  }

  _clear() {
    this.cryptoKey = null;
    if (this.rawMasterKey) this.rawMasterKey.fill(0);
    this.rawMasterKey = null;
    this.hasKey = false;
  }

  async handle(msg, now = Date.now()) {
    if (
      !isPlainObject(msg) ||
      typeof msg.type !== "string" ||
      typeof msg.id !== "number"
    ) {
      throw new Error("invalid message shape");
    }
    switch (msg.type) {
      case "generateKey": {
        // 注意: 既に MK が設定済みでも上書きする。呼び出し側で hasKey
        // チェックすべき (上位 API SharedCryptoClient.generateKey() の
        // JSDoc に警告あり)。
        const raw = crypto.getRandomValues(new Uint8Array(32));
        try {
          await this._setRaw(raw);
        } finally {
          raw.fill(0);
        }
        this.lastActivity = now;
        return { result: { ok: true, keyBits: 256 }, broadcast: "mkChanged" };
      }
      case "setKey": {
        const raw = msg.rawKey;
        try {
          await this._setRaw(raw);
        } finally {
          if (isUint8(raw)) raw.fill(0);
        }
        this.lastActivity = now;
        return { result: { ok: true }, broadcast: "mkChanged" };
      }
      case "clearKey": {
        this._clear();
        // 既に未設定だった場合も broadcast する (新規タブの状態同期を促す)
        return { result: { ok: true }, broadcast: "mkCleared" };
      }
      case "encrypt": {
        if (this.cryptoKey === null) throw new Error("master key not set");
        const r = await aesGcmEncrypt(this.cryptoKey, msg.plaintext, msg.aad);
        this.lastActivity = now;
        return {
          result: { ok: true, iv: r.iv, ciphertext: r.ciphertext },
          broadcast: null,
        };
      }
      case "decrypt": {
        if (this.cryptoKey === null) throw new Error("master key not set");
        const pt = await aesGcmDecrypt(
          this.cryptoKey, msg.ciphertext, msg.iv, msg.aad,
        );
        this.lastActivity = now;
        return { result: { ok: true, plaintext: pt }, broadcast: null };
      }
      case "wrap": {
        if (this.rawMasterKey === null) throw new Error("master key not set");
        // await 前にスナップショットを取る。await 中に clearKey が割り込むと
        // this.rawMasterKey が all-zero になる/null になるため、wrap 対象を確定。
        const snapshot = this.rawMasterKey.slice();
        try {
          const r = await wrapMasterKey(snapshot, msg.derivedKey);
          this.lastActivity = now;
          return {
            result: { ok: true, iv: r.iv, wrapped: r.ciphertext },
            broadcast: null,
          };
        } finally {
          snapshot.fill(0);
          if (isUint8(msg.derivedKey)) msg.derivedKey.fill(0);
        }
      }
      case "unwrap": {
        let rawMk;
        try {
          rawMk = await unwrapMasterKey(
            msg.wrapped, msg.iv, msg.derivedKey,
          );
        } finally {
          // unwrap 失敗 (タグ検証 NG = パスフレーズ誤り等) でも derivedKey はゼロ埋め
          if (isUint8(msg.derivedKey)) msg.derivedKey.fill(0);
        }
        try {
          await this._setRaw(rawMk);
        } finally {
          rawMk.fill(0);
        }
        this.lastActivity = now;
        return { result: { ok: true, keyBits: 256 }, broadcast: "mkChanged" };
      }
      case "touch": {
        this.lastActivity = now;
        return {
          result: { ok: true, hasKey: this.hasKey },
          broadcast: null,
        };
      }
      case "status": {
        return {
          result: {
            ok: true,
            hasKey: this.hasKey,
            lastActivity: this.lastActivity,
            idleMs: this.hasKey ? Math.max(0, now - this.lastActivity) : 0,
          },
          broadcast: null,
        };
      }
      default:
        throw new Error(`unknown type: ${msg.type}`);
    }
  }

  /**
   * idle 検知。鍵が立っていて最終アクティビティから limitMs 超過なら clear。
   * 返値: true=クリアした, false=しなかった (鍵未設定 or 未経過)
   */
  checkIdle(now = Date.now(), limitMs = IDLE_LIMIT_MS) {
    if (!this.hasKey) return false;
    if (now - this.lastActivity < limitMs) return false;
    this._clear();
    return true;
  }
}
