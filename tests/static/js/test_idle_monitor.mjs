// idle-monitor.js (IdleMonitor) の Node 単体テスト。
//
// EventTarget を DI して window/document に依存せず検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD_URL = new URL(
  "../../../app/static/js/crypto/idle-monitor.js",
  import.meta.url,
);
const { IdleMonitor } = await import(MOD_URL.href);


/** touch 呼び出しを記録するスタブクライアント。 */
function makeStubClient() {
  const calls = [];
  return {
    calls,
    touch: () => {
      calls.push(Date.now());
      return Promise.resolve({ ok: true });
    },
  };
}


/** 簡易 EventTarget (visibilitychange 用に hidden プロパティも持つ). */
function makeStubDoc() {
  const target = new EventTarget();
  target.hidden = false;
  return target;
}


test("constructor: client.touch が無いと throw", () => {
  assert.throws(() => new IdleMonitor({}), /client.touch is required/);
  assert.throws(() => new IdleMonitor(null), /client.touch is required/);
});


test("start で初回 touch が即時発火する", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 1000,
    now: () => 1000,
  });
  m.start();
  assert.equal(client.calls.length, 1);
  m.stop();
});


test("ユーザーイベントで touch が発火する", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
    events: ["mousedown", "keydown"],
  });
  m.start();
  assert.equal(client.calls.length, 1); // 初回 touch
  t = 1500; // throttle 超過
  target.dispatchEvent(new Event("mousedown"));
  assert.equal(client.calls.length, 2);
  t = 2000;
  target.dispatchEvent(new Event("keydown"));
  assert.equal(client.calls.length, 3);
  m.stop();
});


test("throttle 内の連続イベントは無視される", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 1000,
    now: () => t,
    events: ["mousedown"],
  });
  m.start();
  assert.equal(client.calls.length, 1); // 初回
  // 同じ時刻で連発
  for (let i = 0; i < 10; i++) {
    target.dispatchEvent(new Event("mousedown"));
  }
  assert.equal(client.calls.length, 1); // throttle で 1 回のまま
  // 時刻が throttle を超えてから 1 回
  t = 2000;
  target.dispatchEvent(new Event("mousedown"));
  assert.equal(client.calls.length, 2);
  m.stop();
});


test("visibilitychange (hidden=false) で touch が発火", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
  });
  m.start();
  client.calls.length = 0; // 初回 touch を捨てる
  t = 1500;
  doc.dispatchEvent(new Event("visibilitychange"));
  assert.equal(client.calls.length, 1);
  m.stop();
});


test("visibilitychange (hidden=true) では touch しない", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
  });
  m.start();
  client.calls.length = 0;
  t = 1500;
  doc.hidden = true;
  doc.dispatchEvent(new Event("visibilitychange"));
  assert.equal(client.calls.length, 0);
  m.stop();
});


test("stop 後はイベントが来ても touch しない", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
    events: ["mousedown"],
  });
  m.start();
  m.stop();
  client.calls.length = 0;
  t = 5000;
  target.dispatchEvent(new Event("mousedown"));
  assert.equal(client.calls.length, 0);
});


test("start() 二重呼び出しはリスナーを二重登録しない", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
    events: ["mousedown"],
  });
  m.start();
  m.start(); // 二度目は no-op
  client.calls.length = 0;
  t = 1500;
  target.dispatchEvent(new Event("mousedown"));
  // 二重登録されていれば 2 回呼ばれる。1 回だけなら OK
  assert.equal(client.calls.length, 1);
  m.stop();
});


test("stop() → start() で再起動して touch が再び動く", () => {
  const client = makeStubClient();
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
    events: ["mousedown"],
  });
  m.start();
  assert.equal(client.calls.length, 1); // 初回 touch
  m.stop();
  client.calls.length = 0;

  // 再起動
  t = 2000;
  m.start();
  // 初回 touch (force=true) が発火するはず
  assert.equal(client.calls.length, 1);

  // 通常のイベントも届く
  t = 3000;
  target.dispatchEvent(new Event("mousedown"));
  assert.equal(client.calls.length, 2);
  m.stop();
});


test("client.touch が reject しても IdleMonitor は止まらない", async () => {
  const calls = [];
  const client = {
    touch: () => {
      calls.push("x");
      return Promise.reject(new Error("network"));
    },
  };
  const target = new EventTarget();
  const doc = makeStubDoc();
  let t = 1000;
  const m = new IdleMonitor(client, {
    target, docTarget: doc, throttleMs: 100,
    now: () => t,
    events: ["mousedown"],
  });
  m.start();
  await new Promise((r) => setTimeout(r, 10)); // microtask flush
  t = 1500;
  target.dispatchEvent(new Event("mousedown"));
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(calls.length, 2);
  m.stop();
});
