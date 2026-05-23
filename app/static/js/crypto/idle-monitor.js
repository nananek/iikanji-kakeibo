// メインスレッド側アクティビティ検知 → SharedWorker への touch 通知
// (設計書 §10.7)。
//
// 役割:
// - mousedown / keydown / touchstart / scroll / visibilitychange を購読し、
//   ユーザー操作があれば SharedWorker に `touch` を送る
// - SharedWorker 側で「全タブの最終アクティビティから 60 分」で idle 判定
// - 大量イベントで postMessage が溢れないよう、throttleMs (デフォルト 1 分)
//   未満の touch は無視
//
// 注意:
// - DI 可能 (CryptoClientLike を受け取る) 設計のため Node でも単体テスト可能
// - addEventListener の target は構築時に渡す (window) を使う。テストでは
//   EventTarget スタブを差し込む

const DEFAULT_THROTTLE_MS = 60 * 1000;
const DEFAULT_EVENTS = ["mousedown", "keydown", "touchstart", "scroll"];

/**
 * `client.touch()` を呼べるオブジェクトを受け取り、ユーザー操作で touch を発火する。
 *
 * @typedef {Object} CryptoClientLike
 * @property {() => Promise<unknown>} touch
 */

export class IdleMonitor {
  #client;
  #target;
  #docTarget;
  #now;
  #handlers = [];
  #lastTouch = 0;
  #stopped = false;
  #started = false;

  /**
   * @param {CryptoClientLike} client          touch() を持つ SharedCryptoClient 等
   * @param {Object} opts
   * @param {EventTarget} [opts.target]        購読対象 (デフォルト: window)
   * @param {EventTarget} [opts.docTarget]     visibilitychange 用 (デフォルト: document)
   * @param {string[]}    [opts.events]        アクティビティ判定対象イベント
   * @param {number}      [opts.throttleMs]    touch スロットル (デフォルト 1 分)
   * @param {() => number}[opts.now]           時刻関数 (テスト注入用、デフォルト Date.now)
   */
  constructor(client, opts = {}) {
    if (!client || typeof client.touch !== "function") {
      throw new Error("client.touch is required");
    }
    this.#client = client;
    this.#target =
      opts.target ?? (typeof window !== "undefined" ? window : null);
    this.#docTarget =
      opts.docTarget ?? (typeof document !== "undefined" ? document : null);
    this.#now = opts.now ?? (() => Date.now());
    this.events = opts.events ?? DEFAULT_EVENTS;
    this.throttleMs = opts.throttleMs ?? DEFAULT_THROTTLE_MS;
  }

  /**
   * 購読開始。初回 touch を即時発火する (last activity を確定するため)。
   * 二重呼び出しは no-op (リスナー二重登録防止)。
   * stop() 後の再起動もサポート (#stopped も合わせてリセット)。
   */
  start() {
    if (this.#started) return;
    this.#stopped = false;  // stop → start の再起動を有効化
    this.#started = true;
    if (this.#target) {
      const opts = { passive: true, capture: true };
      const onActivity = () => this.touch();
      for (const ev of this.events) {
        this.#target.addEventListener(ev, onActivity, opts);
        this.#handlers.push({
          target: this.#target,
          event: ev,
          fn: onActivity,
          opts,
        });
      }
    }
    if (this.#docTarget) {
      const onVis = () => {
        // ハンドラ内で hidden プロパティを直接参照 (なければ常に touch)
        const hidden = this.#docTarget && "hidden" in this.#docTarget
          ? this.#docTarget.hidden
          : false;
        if (!hidden) this.touch();
      };
      this.#docTarget.addEventListener("visibilitychange", onVis);
      this.#handlers.push({
        target: this.#docTarget,
        event: "visibilitychange",
        fn: onVis,
      });
    }
    // 初回 touch (lastActivity を確定させる)
    this.touch(true);
  }

  /**
   * touch を発火。throttleMs 未満は無視。`force=true` で強制発火 (start 時等)。
   */
  touch(force = false) {
    if (this.#stopped) return;
    const now = this.#now();
    if (!force && now - this.#lastTouch < this.throttleMs) return;
    this.#lastTouch = now;
    Promise.resolve(this.#client.touch()).catch(() => {
      // touch 失敗 (Worker 切断等) は IdleMonitor の責務外
    });
  }

  stop() {
    this.#stopped = true;
    this.#started = false;
    for (const h of this.#handlers) {
      try {
        if (h.opts) h.target.removeEventListener(h.event, h.fn, h.opts);
        else h.target.removeEventListener(h.event, h.fn);
      } catch (_e) {
        // target が既に detach されている場合
      }
    }
    this.#handlers = [];
  }
}
