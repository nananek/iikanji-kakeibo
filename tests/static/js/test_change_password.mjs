// #385 PR-4: change_password.mjs の runChangePassword 単体テスト (DOM 不要、DI)。

import { test } from "node:test";
import assert from "node:assert/strict";

const URL_ = new URL("../../../app/static/js/auth/change_password.mjs", import.meta.url);
const { runChangePassword } = await import(URL_.href);


function jsonResponse(status, body = {}) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

function fakeClient({ unwrapThrows = false } = {}) {
  return {
    log: [],
    async unwrap() {
      this.log.push("unwrap");
      if (unwrapThrows) throw new Error("bad tag");
    },
    async wrap() {
      this.log.push("wrap");
      return { wrapped: new Uint8Array(48), iv: new Uint8Array(12) };
    },
  };
}

function baseDeps(overrides = {}) {
  return {
    loadHashWasm: async () => {},
    listWrappedKeys: async () => [
      { method: "passphrase", salt: new Uint8Array(16).fill(1),
        kdf_params: { memory: 65536, iterations: 3, parallelism: 1 },
        wrapped_master_key: new Uint8Array(48), wrap_iv: new Uint8Array(12) },
    ],
    deriveLoginMaterial: async () => ({
      loginVerifier: new Uint8Array(32).fill(0xaa),
      mkWrapKey: new Uint8Array(32).fill(0xbb),
    }),
    generateSalt: () => new Uint8Array(16).fill(7),
    ...overrides,
  };
}


test("成功: unwrap(旧PW検証) → wrap → POST 200 → ok", async () => {
  let body = null;
  const fetchImpl = async (_u, opts) => { body = JSON.parse(opts.body); return jsonResponse(200, { ok: true }); };
  const client = fakeClient();
  const r = await runChangePassword({
    oldPassword: "oldpassword", newPassword: "newpassword", client,
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.deepEqual(r, { status: "ok" });
  assert.deepEqual(client.log, ["unwrap", "wrap"]);
  // 旧/新 login_verifier + 再 wrap 済 material を送る
  assert.ok(body.old_login_verifier && body.login_verifier);
  assert.ok(body.wrapped_master_key && body.wrap_iv && body.login_salt);
});


test("旧パスワード誤り: unwrap 失敗 → wrong_password (POST しない)", async () => {
  let posted = false;
  const fetchImpl = async () => { posted = true; return jsonResponse(200); };
  const r = await runChangePassword({
    oldPassword: "wrong", newPassword: "newpassword", client: fakeClient({ unwrapThrows: true }),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "wrong_password");
  assert.equal(posted, false);
});


test("サーバ 401: wrong_password", async () => {
  const fetchImpl = async () => jsonResponse(401, { error: "x" });
  const r = await runChangePassword({
    oldPassword: "old", newPassword: "newpassword", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "wrong_password");
});


test("新パスワード 8 文字未満: error (派生も unwrap もしない)", async () => {
  let called = false;
  const r = await runChangePassword({
    oldPassword: "old", newPassword: "short", client: fakeClient(),
    deps: { ...baseDeps({ loadHashWasm: async () => { called = true; } }) },
  });
  assert.equal(r.status, "error");
  assert.equal(called, false);
});


test("passphrase 鍵が無い: error", async () => {
  const r = await runChangePassword({
    oldPassword: "old", newPassword: "newpassword", client: fakeClient(),
    deps: { fetchImpl: async () => jsonResponse(200), ...baseDeps({ listWrappedKeys: async () => [] }) },
  });
  assert.equal(r.status, "error");
});


test("サーバ 500: error", async () => {
  const fetchImpl = async () => jsonResponse(500, {});
  const r = await runChangePassword({
    oldPassword: "old", newPassword: "newpassword", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "error");
});
