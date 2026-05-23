// E2EE Master Key 共有 SharedWorker (設計書 §10.7)。
//
// 同一 origin の全タブから MessagePort 経由で接続され、Master Key を
// SharedWorker クロージャ内に保持する。タブが 1 つでも生きていれば
// リロード跨ぎで MK が維持される。ブラウザを全タブ閉じれば MK は消滅
// (再認証が必要)。
//
// 機能:
// - 各タブからの MK 操作 (Dedicated Worker と同じ API: setKey/encrypt/wrap 等)
// - MK 状態変化 (mkChanged / mkCleared) を全接続ポートに broadcast
// - 60 分間アクティビティなしで自動 clearKey (idle 自動ロック)
// - 接続時に現在の状態 (鍵有無) を新規タブに通知 (リロード後の同期)
//
// セキュリティ:
// - MK の raw bytes はメインスレッドに postMessage されない (CryptoKey 越しのみ)
// - 異なる origin からの SharedWorker 接続は不可 (Same-Origin Policy)
// - XSS 1 件で MK 漏洩しないよう、API は限定された operation のみ

import {
  IDLE_CHECK_INTERVAL_MS,
  IDLE_LIMIT_MS,
  MasterKeyState,
} from "./shared-worker-core.js";

const state = new MasterKeyState();
const ports = new Set();

function broadcast(eventName) {
  for (const port of ports) {
    try {
      port.postMessage({ event: eventName });
    } catch (_e) {
      // ポートが既に閉じられている場合は次の onmessageerror で除去される
    }
  }
}

self.onconnect = (ev) => {
  const port = ev.ports[0];
  ports.add(port);

  port.onmessage = async (mev) => {
    const msg = mev.data;
    const id =
      msg && typeof msg.id === "number" ? msg.id : -1;
    try {
      const { result, broadcast: bcast } = await state.handle(msg);
      port.postMessage({ id, ...result });
      if (bcast) broadcast(bcast);
    } catch (e) {
      port.postMessage({
        id,
        ok: false,
        error: String((e && e.message) || e),
      });
    }
  };

  port.onmessageerror = () => {
    ports.delete(port);
  };

  port.start();

  // 接続直後に現在の鍵有無を通知 (新規タブのロック状態同期)
  port.postMessage({
    event: state.hasKey ? "mkChanged" : "mkCleared",
  });
};

// idle タイマー。60 分どのタブからもアクティビティ (touch / encrypt 等) が
// なければ自動的に MK をクリアし、全タブに mkCleared を通知する。
setInterval(() => {
  if (state.checkIdle(Date.now(), IDLE_LIMIT_MS)) {
    broadcast("mkCleared");
  }
}, IDLE_CHECK_INTERVAL_MS);
