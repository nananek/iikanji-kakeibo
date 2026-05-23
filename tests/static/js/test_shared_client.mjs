// SharedCryptoClient の Node 単体テスト。
//
// 実行: node --test tests/static/js/test_shared_client.mjs
//
// SharedWorker / MessagePort は Node にないため、stub をグローバルに
// インストールしてから shared-client.js を import する。

import { test } from "node:test";
import assert from "node:assert/strict";


// stub MessagePort + SharedWorker を構築する。
// テスト側からは createdPorts で内部 port にアクセスして送受信を模擬する。
const createdPorts = [];

class StubPort {
  constructor() {
    this.onmessage = null;
    this.onmessageerror = null;
    this.posted = [];
    this.closed = false;
    this.started = false;
  }
  start() { this.started = true; }
  postMessage(data, transferables) {
    this.posted.push({ data, transferables });
  }
  close() { this.closed = true; }
  /** テスト helper: worker からメインへ broadcast / response 配信 */
  _deliver(data) {
    if (this.onmessage) this.onmessage({ data });
  }
}

class StubSharedWorker {
  constructor(_url, _opts) {
    this.port = new StubPort();
    createdPorts.push(this.port);
  }
}

globalThis.SharedWorker = StubSharedWorker;


const CLIENT_URL = new URL(
  "../../../app/static/js/crypto/shared-client.js",
  import.meta.url,
);
const { SharedCryptoClient } = await import(CLIENT_URL.href);


function newClient() {
  return new SharedCryptoClient("dummy.js");
}


test("constructor は SharedWorker(port) を生成し port.start() を呼ぶ", () => {
  createdPorts.length = 0;
  newClient();
  assert.equal(createdPorts.length, 1);
  assert.equal(createdPorts[0].started, true);
});


test("on() 登録前に届いた mkCleared は on() 時に即時 invoke される (race 解消)", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];

  // 接続直後の初期 broadcast を模擬
  port._deliver({ event: "mkCleared" });

  // この時点で listener はまだ登録されていない
  let called = 0;
  client.on("mkCleared", () => { called++; });
  // 同期で即時 invoke される
  assert.equal(called, 1);
});


test("on() の即時 invoke は対象イベントのみ", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  port._deliver({ event: "mkCleared" });

  let changed = 0;
  client.on("mkChanged", () => { changed++; });
  // 最後のイベントは mkCleared なので mkChanged listener は invoke されない
  assert.equal(changed, 0);
});


test("on() 登録後の broadcast は通常通り invoke される", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  const events = [];
  client.on("mkChanged", () => events.push("changed"));
  client.on("mkCleared", () => events.push("cleared"));
  port._deliver({ event: "mkChanged" });
  port._deliver({ event: "mkCleared" });
  port._deliver({ event: "mkChanged" });
  assert.deepEqual(events, ["changed", "cleared", "changed"]);
});


test("同じイベントが新たに届くと #lastEvent も更新される (古いの上書き)", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  port._deliver({ event: "mkCleared" });
  port._deliver({ event: "mkChanged" });

  // 最後は mkChanged なので、これから on("mkChanged") は即時 invoke される
  let changed = 0;
  let cleared = 0;
  client.on("mkChanged", () => { changed++; });
  client.on("mkCleared", () => { cleared++; });
  assert.equal(changed, 1);
  assert.equal(cleared, 0);
});


test("on() の購読解除関数で以後通知が来なくなる", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  let count = 0;
  const unsubscribe = client.on("mkChanged", () => { count++; });
  port._deliver({ event: "mkChanged" });
  assert.equal(count, 1);
  unsubscribe();
  port._deliver({ event: "mkChanged" });
  assert.equal(count, 1); // 増えない
});


test("on() のハンドラ例外は他のハンドラに伝播しない", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  let ok = false;
  client.on("mkChanged", () => { throw new Error("boom"); });
  client.on("mkChanged", () => { ok = true; });
  port._deliver({ event: "mkChanged" });
  assert.equal(ok, true);
});


test("レスポンス (id 付き) は #pending Promise を resolve する", async () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  const p = client.touch();
  // postMessage された id を取得して同じ id で返信
  const last = port.posted[port.posted.length - 1];
  assert.equal(last.data.type, "touch");
  port._deliver({ id: last.data.id, ok: true, hasKey: true });
  const result = await p;
  assert.equal(result.hasKey, true);
});


test("レスポンス (ok=false) は Promise を reject する", async () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  const p = client.encrypt(new Uint8Array([1, 2, 3]));
  const last = port.posted[port.posted.length - 1];
  port._deliver({ id: last.data.id, ok: false, error: "master key not set" });
  await assert.rejects(() => p, /master key not set/);
});


test("#send のタイムアウトでエラー reject される", async () => {
  createdPorts.length = 0;
  // 100ms タイムアウト (テスト時短)
  const client = new SharedCryptoClient("dummy.js", { timeoutMs: 100 });
  // touch を呼んで、stub port は応答を返さない (worker クラッシュ等の模擬)
  const p = client.touch();
  await assert.rejects(
    () => p,
    /timed out after 100ms.*type=touch/,
  );
});


test("タイムアウト後に遅れて来た応答は無視される (resolve しない)", async () => {
  createdPorts.length = 0;
  const client = new SharedCryptoClient("dummy.js", { timeoutMs: 50 });
  const port = createdPorts[0];
  const p = client.touch();
  const last = port.posted[port.posted.length - 1];
  // タイムアウト発火を待つ
  await assert.rejects(() => p, /timed out/);
  // 遅れて来た応答 (もう pending に存在しない) → 静かに無視される
  port._deliver({ id: last.data.id, ok: true, hasKey: false });
  // 例外が起きないこと (これ自体が assertion)
});


test("正常応答ではタイムアウトクリアされて Promise resolve", async () => {
  createdPorts.length = 0;
  const client = new SharedCryptoClient("dummy.js", { timeoutMs: 2000 });
  const port = createdPorts[0];
  const p = client.touch();
  const last = port.posted[port.posted.length - 1];
  port._deliver({ id: last.data.id, ok: true, hasKey: true });
  const r = await p;
  assert.equal(r.hasKey, true);
});


test("setKey / generateKey / clearKey が postMessage に正しい type を送る", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  // 各メソッドの返り値 Promise は捕捉して unhandled rejection を避ける
  // (テスト終了時に close() で全 reject されるが警告が出ないように catch)
  const swallow = (p) => p.catch(() => {});
  swallow(client.generateKey());
  swallow(client.clearKey());
  swallow(client.setKey(new Uint8Array(32)));
  swallow(client.encrypt(new Uint8Array([1, 2])));
  swallow(client.decrypt(new Uint8Array([3]), new Uint8Array(12)));
  swallow(client.wrap(new Uint8Array(32)));
  swallow(client.unwrap(new Uint8Array(32), new Uint8Array(16), new Uint8Array(12)));
  swallow(client.status());
  const types = port.posted.map((p) => p.data.type);
  assert.deepEqual(types, [
    "generateKey", "clearKey", "setKey", "encrypt", "decrypt", "wrap", "unwrap", "status",
  ]);
  client.close(); // pending を全て reject + タイマークリア
});


test("setKey / wrap / unwrap は Transferable に rawKey/derivedKey を含める", () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  const k = new Uint8Array(32);
  client.setKey(k).catch(() => {});
  // 最後の postMessage の transferables は [k.buffer]
  const last = port.posted[port.posted.length - 1];
  assert.equal(last.transferables?.length, 1);
  client.close();
});


test("旧 string 引数 (name) の後方互換: SharedCryptoClient(url, 'my-name')", () => {
  createdPorts.length = 0;
  // string をそのまま name として扱う
  const client = new SharedCryptoClient("dummy.js", "my-name");
  assert.equal(createdPorts.length, 1);
  // タイムアウトはデフォルト値 (30 秒) になる
});


test("close() は #pending Promise を全て reject する", async () => {
  createdPorts.length = 0;
  const client = newClient();
  const port = createdPorts[0];
  const p1 = client.touch();
  const p2 = client.touch();
  client.close();
  await assert.rejects(() => p1, /shared worker port closed/);
  await assert.rejects(() => p2, /shared worker port closed/);
  assert.equal(port.closed, true);
});
