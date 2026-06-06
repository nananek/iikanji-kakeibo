// #385 PR-3a: login_flow.mjs の runLoginFlow / safeNextUrl 単体テスト (DOM 不要、DI)。

import { test } from "node:test";
import assert from "node:assert/strict";

const URL_ = new URL("../../../app/static/js/auth/login_flow.mjs", import.meta.url);
const { runLoginFlow, safeNextUrl } = await import(URL_.href);


function jsonResponse(status, body) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: async () => body,
  };
}


// begin/finish の応答を差し込める fake fetch。
function makeFetch(routes) {
  const calls = [];
  const fetchImpl = async (url, opts) => {
    calls.push({ url, body: opts?.body ? JSON.parse(opts.body) : null });
    const handler = routes[url];
    if (!handler) throw new Error(`no route for ${url}`);
    return handler(calls.length, calls);
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}


function fakeClient() {
  return {
    log: [],
    async generateKey() { this.log.push("generateKey"); },
    async wrap(_k) { this.log.push("wrap"); return { wrapped: new Uint8Array(48), iv: new Uint8Array(12) }; },
    async unwrap(_k, _w, _iv) { this.log.push("unwrap"); },
    async clearKey() { this.log.push("clearKey"); },
    close() { this.log.push("close"); },
  };
}


function baseDeps(overrides = {}) {
  return {
    loadHashWasm: async () => {},
    deriveLoginMaterial: async () => ({
      loginVerifier: new Uint8Array(32).fill(0xaa),
      mkWrapKey: new Uint8Array(32).fill(0xbb),
    }),
    ensureKeyPair: async () => true,
    listWrappedKeys: async () => [
      { method: "passphrase", wrapped_master_key: new Uint8Array(48), wrap_iv: new Uint8Array(12) },
    ],
    runRewrapMigration: async () => ({ active: true, finalized: true }),
    ...overrides,
  };
}


test("safeNextUrl: 内部パスのみ許可、プロトコル相対/外部は / にフォールバック", () => {
  assert.equal(safeNextUrl("?next=/reports"), "/reports");
  assert.equal(safeNextUrl("?next=//evil.com"), "/");
  assert.equal(safeNextUrl("?next=https://evil.com"), "/");
  assert.equal(safeNextUrl("?next="), "/");
  assert.equal(safeNextUrl(""), "/");
});


test("通常ログイン: finish ok → wrapped_keys 取得 → unwrap → redirect", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: false }),
    "/auth/login/finish": () => jsonResponse(200, { ok: true }),
  });
  const client = fakeClient();
  const r = await runLoginFlow({
    username: "u", password: "p", nextUrl: "/dash", client,
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.deepEqual(r, { status: "redirect", url: "/dash" });
  assert.ok(client.log.includes("unwrap"));
  // 通常パスでは password を送らない
  const finishCall = fetchImpl.calls.find((c) => c.url === "/auth/login/finish");
  assert.equal(finishCall.body.password, undefined);
  assert.ok(finishCall.body.login_verifier);
});


test("通常ログイン: passphrase wrapped_key 不在ならエラー", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: false }),
    "/auth/login/finish": () => jsonResponse(200, { ok: true }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps({ listWrappedKeys: async () => [] }) },
  });
  assert.equal(r.status, "error");
});


test("通常ログイン: finish 401 → エラー", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: false }),
    "/auth/login/finish": () => jsonResponse(401, { error: "x" }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "error");
});


test("移行ログイン: generateKey→wrap→finish→ensureKeyPair→rewrap→redirect", async () => {
  let rewrapArgs = null;
  let keypairArgs = null;
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: { memory: 65536 }, migration_required: true }),
    "/auth/login/finish": () => jsonResponse(200, { ok: true, migrated: true, user_id: 7, needs_rewrap: true, years: [2025] }),
  });
  const client = fakeClient();
  const r = await runLoginFlow({
    username: "u", password: "p", nextUrl: "/", client,
    deps: {
      fetchImpl,
      ...baseDeps({
        ensureKeyPair: async (c, uid) => { keypairArgs = { uid }; return true; },
        runRewrapMigration: async (a) => { rewrapArgs = a; return { active: true }; },
      }),
    },
  });
  assert.deepEqual(r, { status: "redirect", url: "/" });
  assert.deepEqual(client.log.slice(0, 2), ["generateKey", "wrap"]);
  assert.equal(keypairArgs.uid, 7);
  assert.equal(rewrapArgs.userId, 7);
  assert.deepEqual(rewrapArgs.years, [2025]);
  // 移行パスでは password を送る
  const finishCall = fetchImpl.calls.find((c) => c.url === "/auth/login/finish");
  assert.equal(finishCall.body.password, "p");
  assert.ok(finishCall.body.wrapped_master_key);
});


test("通常ログイン: needs_rewrap=true なら rewrap を resume する (中断復帰)", async () => {
  let rewrapArgs = null;
  let keypairCalled = false;
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: false }),
    "/auth/login/finish": () => jsonResponse(200, { ok: true, user_id: 5, needs_rewrap: true, years: [2024] }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: {
      fetchImpl,
      ...baseDeps({
        ensureKeyPair: async () => { keypairCalled = true; return true; },
        runRewrapMigration: async (a) => { rewrapArgs = a; return { active: true }; },
      }),
    },
  });
  assert.equal(r.status, "redirect");
  assert.equal(keypairCalled, true);
  assert.deepEqual(rewrapArgs.years, [2024]);
});


test("移行ログイン: post-finish 例外は致命にせず clearKey + redirect", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: true }),
    "/auth/login/finish": () => jsonResponse(200, { ok: true, user_id: 1, needs_rewrap: true, years: [2025] }),
  });
  const client = fakeClient();
  const r = await runLoginFlow({
    username: "u", password: "p", client,
    deps: {
      fetchImpl,
      ...baseDeps({ ensureKeyPair: async () => { throw new Error("boom"); } }),
    },
  });
  // 認証因子は server-side 確立済なので redirect。Worker の MK は clearKey する。
  assert.equal(r.status, "redirect");
  assert.ok(client.log.includes("clearKey"));
});


test("移行ログイン: needs_rewrap=false なら rewrap を呼ばない", async () => {
  let rewrapCalled = false;
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: true }),
    "/auth/login/finish": () => jsonResponse(200, { ok: true, user_id: 1, needs_rewrap: false, years: [] }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps({ runRewrapMigration: async () => { rewrapCalled = true; } }) },
  });
  assert.equal(r.status, "redirect");
  assert.equal(rewrapCalled, false);
});


test("移行ログイン: finish 失敗 → clearKey + エラー", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: true }),
    "/auth/login/finish": () => jsonResponse(400, { error: "invalid salt" }),
  });
  const client = fakeClient();
  const r = await runLoginFlow({
    username: "u", password: "p", client,
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "error");
  assert.ok(client.log.includes("clearKey"));
});


test("begin 503 → fallback (従来フォーム送信)", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(503, { error: "login not configured" }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.deepEqual(r, { status: "fallback" });
});


test("requires_password_setup → password_setup", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(200, { salt: btoa("0123456789abcdef"), kdf_params: {}, migration_required: true, requires_password_setup: true }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.deepEqual(r, { status: "password_setup" });
});


test("begin 400 → エラー", async () => {
  const fetchImpl = makeFetch({
    "/auth/login/begin": () => jsonResponse(400, { error: "username required" }),
  });
  const r = await runLoginFlow({
    username: "u", password: "p", client: fakeClient(),
    deps: { fetchImpl, ...baseDeps() },
  });
  assert.equal(r.status, "error");
});
