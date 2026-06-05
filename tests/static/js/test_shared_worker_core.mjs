// shared-worker-core.js (MasterKeyState) の Node 単体テスト。
//
// 実行: node --test tests/static/js/test_shared_worker_core.mjs
//
// state は純粋クラスのため Node の WebCrypto API のみで動作する。
// timer (setInterval) や onconnect は shared-worker.js 側に切り出して
// あるためここではテストしない。

import { test } from "node:test";
import assert from "node:assert/strict";

const CORE_URL = new URL(
  "../../../app/static/js/crypto/shared-worker-core.js",
  import.meta.url,
);
const { MasterKeyState, IDLE_LIMIT_MS, IDLE_CHECK_INTERVAL_MS } =
  await import(CORE_URL.href);


function randomBytes(n) {
  return crypto.getRandomValues(new Uint8Array(n));
}

function utf8(s) {
  return new TextEncoder().encode(s);
}

let _id = 1;
function msg(type, extra = {}) {
  return { id: _id++, type, ...extra };
}


test("初期状態は鍵未設定、status は idleMs=0", async () => {
  const state = new MasterKeyState();
  assert.equal(state.hasKey, false);
  const r = await state.handle(msg("status"));
  assert.equal(r.result.hasKey, false);
  assert.equal(r.result.idleMs, 0);
  assert.equal(r.broadcast, null);
});


test("generateKey で hasKey=true + broadcast mkChanged", async () => {
  const state = new MasterKeyState();
  const r = await state.handle(msg("generateKey"), 1000);
  assert.equal(r.result.ok, true);
  assert.equal(r.result.keyBits, 256);
  assert.equal(r.broadcast, "mkChanged");
  assert.equal(state.hasKey, true);
  assert.equal(state.lastActivity, 1000);
});


test("setKey は raw 32B 以外を弾く", async () => {
  const state = new MasterKeyState();
  await assert.rejects(
    () => state.handle(msg("setKey", { rawKey: randomBytes(16) })),
    /must be Uint8Array of 32 bytes/,
  );
  assert.equal(state.hasKey, false);
});


test("encrypt/decrypt 往復で平文一致", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }));
  const enc = await state.handle(
    msg("encrypt", { plaintext: utf8("hello world") }),
  );
  assert.equal(enc.broadcast, null);
  assert.equal(enc.result.iv.byteLength, 12);
  const dec = await state.handle(
    msg("decrypt", { ciphertext: enc.result.ciphertext, iv: enc.result.iv }),
  );
  assert.deepEqual([...dec.result.plaintext], [...utf8("hello world")]);
});


test("encrypt/decrypt は AAD 不一致で失敗", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }));
  const enc = await state.handle(
    msg("encrypt", { plaintext: utf8("x"), aad: utf8("ctx:1") }),
  );
  await assert.rejects(() =>
    state.handle(msg("decrypt", {
      ciphertext: enc.result.ciphertext,
      iv: enc.result.iv,
      aad: utf8("ctx:2"),
    })),
  );
});


test("clearKey で hasKey=false + broadcast mkCleared", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("generateKey"));
  const r = await state.handle(msg("clearKey"));
  assert.equal(r.broadcast, "mkCleared");
  assert.equal(state.hasKey, false);
  // clear 後 encrypt は失敗
  await assert.rejects(
    () => state.handle(msg("encrypt", { plaintext: utf8("x") })),
    /master key not set/,
  );
});


test("clearKey は鍵未設定でも broadcast する (新規タブ同期用)", async () => {
  const state = new MasterKeyState();
  const r = await state.handle(msg("clearKey"));
  assert.equal(r.broadcast, "mkCleared");
});


test("MK 未設定で encrypt/decrypt/wrap は失敗", async () => {
  const state = new MasterKeyState();
  await assert.rejects(
    () => state.handle(msg("encrypt", { plaintext: utf8("x") })),
    /master key not set/,
  );
  await assert.rejects(
    () => state.handle(msg("decrypt", {
      ciphertext: new Uint8Array(16), iv: randomBytes(12),
    })),
    /master key not set/,
  );
  await assert.rejects(
    () => state.handle(msg("wrap", { derivedKey: randomBytes(32) })),
    /master key not set/,
  );
});


test("wrap → 別 state で unwrap して MK 復元", async () => {
  const s1 = new MasterKeyState();
  await s1.handle(msg("generateKey"));
  const wk = randomBytes(32);
  const wkCopy = new Uint8Array(wk);
  const w = await s1.handle(msg("wrap", { derivedKey: wk }));
  assert.equal(w.broadcast, null);

  const s2 = new MasterKeyState();
  const u = await s2.handle(msg("unwrap", {
    derivedKey: wkCopy,
    wrapped: w.result.wrapped,
    iv: w.result.iv,
  }));
  assert.equal(u.result.ok, true);
  assert.equal(u.broadcast, "mkChanged");
  assert.equal(s2.hasKey, true);

  // s1 / s2 とも同じ平文を復号できるはず
  const enc = await s1.handle(
    msg("encrypt", { plaintext: utf8("data") }),
  );
  const dec = await s2.handle(
    msg("decrypt", { ciphertext: enc.result.ciphertext, iv: enc.result.iv }),
  );
  assert.deepEqual([...dec.result.plaintext], [...utf8("data")]);
});


test("unwrap は誤った derivedKey で失敗し state は未設定のまま", async () => {
  const s1 = new MasterKeyState();
  await s1.handle(msg("generateKey"));
  const wk = randomBytes(32);
  const w = await s1.handle(msg("wrap", { derivedKey: new Uint8Array(wk) }));

  const s2 = new MasterKeyState();
  await assert.rejects(() =>
    s2.handle(msg("unwrap", {
      derivedKey: randomBytes(32),  // 違う鍵
      wrapped: w.result.wrapped,
      iv: w.result.iv,
    })),
  );
  assert.equal(s2.hasKey, false);
});


test("touch でアクティビティ時刻が更新される", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("generateKey"), 1000);
  assert.equal(state.lastActivity, 1000);
  const r = await state.handle(msg("touch"), 5000);
  assert.equal(state.lastActivity, 5000);
  assert.equal(r.result.hasKey, true);
  assert.equal(r.broadcast, null);
});


test("encrypt もアクティビティ時刻を更新する", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }), 1000);
  await state.handle(msg("encrypt", { plaintext: utf8("x") }), 8000);
  assert.equal(state.lastActivity, 8000);
});


test("checkIdle: 鍵あり・タイムアウト前は false", () => {
  const state = new MasterKeyState();
  state.hasKey = true;
  state.lastActivity = 1000;
  assert.equal(state.checkIdle(1000 + 999, 1000), false);
  assert.equal(state.hasKey, true);
});


test("checkIdle: 鍵あり・タイムアウト超過で clear & true", () => {
  const state = new MasterKeyState();
  state.hasKey = true;
  state.lastActivity = 1000;
  // 等価境界 (now - last = limit) もタイムアウト扱い
  assert.equal(state.checkIdle(1000 + 1000, 1000), true);
  assert.equal(state.hasKey, false);
  assert.equal(state.cryptoKey, null);
  assert.equal(state.rawMasterKey, null);
});


test("checkIdle: 鍵未設定なら何もしない", () => {
  const state = new MasterKeyState();
  assert.equal(state.checkIdle(Date.now(), IDLE_LIMIT_MS), false);
});


test("status は idleMs を返す (鍵あり時のみ)", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("generateKey"), 1000);
  const r = await state.handle(msg("status"), 5000);
  assert.equal(r.result.hasKey, true);
  assert.equal(r.result.idleMs, 4000);
  assert.equal(r.result.lastActivity, 1000);
});


test("不正なメッセージ型・未知の type は throw", async () => {
  const state = new MasterKeyState();
  await assert.rejects(() => state.handle({}), /invalid message shape/);
  await assert.rejects(
    () => state.handle({ id: 1, type: "xxx" }),
    /unknown type: xxx/,
  );
});


test("定数値: IDLE_LIMIT_MS=60min, IDLE_CHECK_INTERVAL_MS=1min", () => {
  assert.equal(IDLE_LIMIT_MS, 60 * 60 * 1000);
  assert.equal(IDLE_CHECK_INTERVAL_MS, 60 * 1000);
});


// ── E7 dual-key 再ラップ (#114) ──────────────────────────────────────
//
// temp-MK で暗号化されたデータを Worker 内で本物 MK に再ラップする。
// shared-client.js (SharedCryptoClient) の setRewrapKey/rewrap/clearRewrapKey
// が経由する handle() のケースを検証する。
//
// テストでは temp-MK 暗号文を生成する必要があるため、別 state に temp-MK を
// setKey して encrypt したものを「サーバが temp-MK で暗号化したデータ」として
// 扱う (実フローではサーバ側 migration_crypto.py が生成)。

async function tempCiphertext(tempRaw, plaintext, aad) {
  // temp-MK で暗号化した {ciphertext, iv} を作る (サーバ側暗号化のシミュレート)。
  const s = new MasterKeyState();
  await s.handle(msg("setKey", { rawKey: new Uint8Array(tempRaw) }));
  const enc = await s.handle(msg("encrypt", { plaintext, aad }));
  return { ciphertext: enc.result.ciphertext, iv: enc.result.iv };
}

test("setRewrapKey は raw 32B 以外を弾く", async () => {
  const state = new MasterKeyState();
  await assert.rejects(
    () => state.handle(msg("setRewrapKey", { rawKey: randomBytes(16) })),
    /must be Uint8Array of 32 bytes/,
  );
  assert.equal(state.rewrapKey, null);
});

test("rewrap: temp-MK 暗号文を本物 MK へ再ラップ → 本物 MK で復号一致", async () => {
  const tempRaw = randomBytes(32);
  const realRaw = randomBytes(32);
  const aad = utf8("je\x00ctx");
  const pt = utf8("secret record");
  const { ciphertext, iv } = await tempCiphertext(tempRaw, pt, aad);

  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: new Uint8Array(realRaw) }));
  await state.handle(msg("setRewrapKey", { rawKey: new Uint8Array(tempRaw) }));

  const re = await state.handle(msg("rewrap", { ciphertext, iv, aad }));
  assert.equal(re.result.ok, true);
  assert.equal(re.result.iv.byteLength, 12);
  // 再暗号化で新 IV が振られる
  assert.notDeepEqual([...re.result.iv], [...iv]);

  // 本物 MK で復号すると元の平文に一致する
  const dec = await state.handle(msg("decrypt", {
    ciphertext: re.result.ciphertext, iv: re.result.iv, aad,
  }));
  assert.deepEqual([...dec.result.plaintext], [...pt]);
});

test("rewrap は AAD 不一致 (改ざん) で失敗する", async () => {
  const tempRaw = randomBytes(32);
  const realRaw = randomBytes(32);
  const { ciphertext, iv } = await tempCiphertext(
    tempRaw, utf8("x"), utf8("je\x00a"),
  );
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: new Uint8Array(realRaw) }));
  await state.handle(msg("setRewrapKey", { rawKey: new Uint8Array(tempRaw) }));
  await assert.rejects(() =>
    state.handle(msg("rewrap", { ciphertext, iv, aad: utf8("je\x00b") })),
  );
});

test("rewrap は本物 MK 未設定で失敗", async () => {
  const tempRaw = randomBytes(32);
  const { ciphertext, iv } = await tempCiphertext(tempRaw, utf8("x"), undefined);
  const state = new MasterKeyState();
  await state.handle(msg("setRewrapKey", { rawKey: new Uint8Array(tempRaw) }));
  await assert.rejects(
    () => state.handle(msg("rewrap", { ciphertext, iv })),
    /master key not set/,
  );
});

test("rewrap は副鍵 (temp-MK) 未設定で失敗", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }));
  await assert.rejects(
    () => state.handle(msg("rewrap", {
      ciphertext: randomBytes(20), iv: randomBytes(12),
    })),
    /rewrap key not set/,
  );
});

test("clearRewrapKey で副鍵だけ破棄 (本物 MK は維持)", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }));
  await state.handle(msg("setRewrapKey", { rawKey: randomBytes(32) }));
  const r = await state.handle(msg("clearRewrapKey"));
  assert.equal(r.result.ok, true);
  assert.equal(r.broadcast, null);
  assert.equal(state.rewrapKey, null);
  assert.equal(state.hasKey, true); // 本物 MK は残る
});

test("clearKey は副鍵 (temp-MK) も同時に破棄する", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }));
  await state.handle(msg("setRewrapKey", { rawKey: randomBytes(32) }));
  await state.handle(msg("clearKey"));
  assert.equal(state.rewrapKey, null);
  assert.equal(state.hasKey, false);
});

test("idle 自動ロックは副鍵 (temp-MK) も破棄する", async () => {
  const state = new MasterKeyState();
  await state.handle(msg("setKey", { rawKey: randomBytes(32) }), 0);
  await state.handle(msg("setRewrapKey", { rawKey: randomBytes(32) }));
  const cleared = state.checkIdle(IDLE_LIMIT_MS + 1, IDLE_LIMIT_MS);
  assert.equal(cleared, true);
  assert.equal(state.rewrapKey, null);
});
