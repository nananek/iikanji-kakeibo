// #385 PR-4b-3: runRecoveryReset の単体テスト (DOM 不要、DI)。

import { test } from "node:test";
import assert from "node:assert/strict";

const URL_ = new URL("../../../app/static/js/auth/recovery_reset.mjs", import.meta.url);
const { runRecoveryReset } = await import(URL_.href);


function jsonResponse(status, body = {}) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

// begin は wrapped_master_key / wrap_iv を返す。値の中身は fakeClient.unwrap が無視する。
function beginBody() {
  return { wrapped_master_key: btoa("x".repeat(48)), wrap_iv: btoa("y".repeat(12)) };
}

function fakeClient({ unwrapThrows = false } = {}) {
  return {
    log: [],
    async unwrap() { this.log.push("unwrap"); if (unwrapThrows) throw new Error("bad tag"); },
    async wrap() {
      this.log.push("wrap");
      return { wrapped: new Uint8Array(48), iv: new Uint8Array(12) };
    },
  };
}

function baseDeps(overrides = {}) {
  return {
    loadHashWasm: async () => {},
    deriveLoginMaterial: async () => ({
      loginVerifier: new Uint8Array(32).fill(0xaa),
      mkWrapKey: new Uint8Array(32).fill(0xbb),
    }),
    deriveKeyFromMnemonic: async () => new Uint8Array(32).fill(0xcc),
    deriveRecoveryVerifier: async () => new Uint8Array(32).fill(0xdd),
    generateMnemonic: async () => "word ".repeat(23) + "art",
    generateSalt: () => new Uint8Array(16).fill(7),
    ...overrides,
  };
}

const VALID = {
  username: "alice", mnemonic: "word ".repeat(23) + "art", newPassword: "newpassword",
};


test("成功: begin→unwrap→wrap×2→finish 200 → ok + 新シード返す", async () => {
  const bodies = [];
  const fetchImpl = async (u, opts) => {
    bodies.push([u, opts ? JSON.parse(opts.body) : null]);
    if (u.endsWith("/begin")) return jsonResponse(200, beginBody());
    return jsonResponse(200, { ok: true });
  };
  const client = fakeClient();
  const r = await runRecoveryReset({ ...VALID, client, deps: { fetchImpl, ...baseDeps() } });
  assert.equal(r.status, "ok");
  assert.ok(r.newMnemonic && r.newMnemonic.split(" ").length === 24);
  // unwrap 1 回 + wrap 2 回 (passphrase, recovery)
  assert.deepEqual(client.log, ["unwrap", "wrap", "wrap"]);
  // finish に必要フィールドが揃っている
  const finishBody = bodies.find(([u]) => u.endsWith("/finish"))[1];
  assert.ok(finishBody.recovery_verifier && finishBody.new_recovery_verifier);
  assert.ok(finishBody.passphrase_wrapped_master_key && finishBody.recovery_wrapped_master_key);
  assert.equal(finishBody.username, "alice");
});


test("シード不一致: unwrap 失敗 → wrong_seed (finish しない)", async () => {
  let finishCalled = false;
  const fetchImpl = async (u) => {
    if (u.endsWith("/begin")) return jsonResponse(200, beginBody());
    finishCalled = true; return jsonResponse(200, { ok: true });
  };
  const r = await runRecoveryReset({
    ...VALID, client: fakeClient({ unwrapThrows: true }), deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "wrong_seed");
  assert.equal(finishCalled, false);
});


test("シード形式不正: deriveKeyFromMnemonic が throw → invalid_seed", async () => {
  const fetchImpl = async (u) => (u.endsWith("/begin") ? jsonResponse(200, beginBody()) : jsonResponse(200));
  const r = await runRecoveryReset({
    ...VALID, client: fakeClient(),
    deps: { fetchImpl, ...baseDeps({ deriveKeyFromMnemonic: async () => { throw new Error("checksum"); } }) },
  });
  assert.equal(r.status, "invalid_seed");
});


test("finish 401 (サーバ側ハッシュ未設定等): wrong_seed", async () => {
  const fetchImpl = async (u) => (u.endsWith("/begin") ? jsonResponse(200, beginBody()) : jsonResponse(401, {}));
  const r = await runRecoveryReset({ ...VALID, client: fakeClient(), deps: { fetchImpl, ...baseDeps() } });
  assert.equal(r.status, "wrong_seed");
});


test("begin 失敗: error", async () => {
  const fetchImpl = async () => jsonResponse(503, {});
  const r = await runRecoveryReset({ ...VALID, client: fakeClient(), deps: { fetchImpl, ...baseDeps() } });
  assert.equal(r.status, "error");
});


test("新パスワード 8 文字未満: error (begin もしない)", async () => {
  let called = false;
  const fetchImpl = async () => { called = true; return jsonResponse(200, beginBody()); };
  const r = await runRecoveryReset({
    ...VALID, newPassword: "short", client: fakeClient(), deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "error");
  assert.equal(called, false);
});


test("username 空: error", async () => {
  const r = await runRecoveryReset({
    ...VALID, username: "", client: fakeClient(),
    deps: { fetchImpl: async () => jsonResponse(200, beginBody()), ...baseDeps() },
  });
  assert.equal(r.status, "error");
});
