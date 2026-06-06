// login_kdf.js (ログイン派生 MK の KDF プリミティブ, PR-1 #385) の Node 単体テスト。
//
// 設計書 docs/v5-e2ee/login-derived-mk.md §2 / §3.2 / §3.5。
//
// golden vector はここで凍結し、client-py / TUI (PR-5) の byte 互換契約とする。
// 値が変わるとテストが落ちる = 仕様変更の検知点。

import { test } from "node:test";
import assert from "node:assert/strict";

const KDF_URL = new URL(
  "../../../app/static/js/crypto/login_kdf.js",
  import.meta.url,
);
const ARGON_URL = new URL(
  "../../../app/static/js/crypto/argon2.js",
  import.meta.url,
);
const VENDOR_URL = new URL(
  "../../../app/static/js/vendor/hash-wasm-4.12.0.esm.min.js",
  import.meta.url,
);

const {
  LOGIN_VERIFIER_INFO,
  MK_WRAP_KEY_INFO,
  hkdfLoginSplit,
  deriveLoginMaterial,
} = await import(KDF_URL.href);
const { setArgon2idImpl } = await import(ARGON_URL.href);

const hex = (u) => Buffer.from(u).toString("hex");

// 決定的 stub (test_argon2.mjs と同方式): SHA-256(password || salt)[0:hashLength]。
function makeStubImpl({ recorder } = {}) {
  return async (opts) => {
    if (recorder) recorder.push(opts);
    const pwBytes =
      typeof opts.password === "string"
        ? new TextEncoder().encode(opts.password)
        : opts.password;
    const combined = new Uint8Array(pwBytes.byteLength + opts.salt.byteLength);
    combined.set(pwBytes, 0);
    combined.set(opts.salt, pwBytes.byteLength);
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", combined));
    return digest.slice(0, opts.hashLength ?? 32);
  };
}

// ------------------------------------------------------------------
// info 定数 (ドメイン分離) は設計書の確定値
// ------------------------------------------------------------------

test("HKDF info 定数は設計書の確定値 (短縮形不可)", () => {
  assert.equal(LOGIN_VERIFIER_INFO, "iikanji-login-v1");
  assert.equal(MK_WRAP_KEY_INFO, "iikanji-mk-wrap-v1");
});

// ------------------------------------------------------------------
// hkdfLoginSplit: 固定 master → golden vector (純粋・決定的)
// ------------------------------------------------------------------

test("hkdfLoginSplit: 固定 master の golden vector (byte 互換契約)", async () => {
  const master = new Uint8Array(32);
  for (let i = 0; i < 32; i++) master[i] = i; // 0x00..0x1f

  const { loginVerifier, mkWrapKey } = await hkdfLoginSplit(master);

  assert.equal(loginVerifier.byteLength, 32);
  assert.equal(mkWrapKey.byteLength, 32);
  assert.equal(
    hex(loginVerifier),
    "5df62d0f9062c895fbb78a5fa74744c747ee3b2611bbffd3e1c6c44633e69e15",
  );
  assert.equal(
    hex(mkWrapKey),
    "c520a22f75dad5f2c2eccfef9363643241f139280e4cef69ec884238c883e12a",
  );
});

test("hkdfLoginSplit: login_verifier と mk_wrap_key はドメイン分離されている", async () => {
  const master = new Uint8Array(32).fill(7);
  const { loginVerifier, mkWrapKey } = await hkdfLoginSplit(master);
  // 同一 master でも info が異なるため一致してはならない
  assert.notDeepEqual([...loginVerifier], [...mkWrapKey]);
});

test("hkdfLoginSplit: 同じ master は決定的に同じ出力", async () => {
  const master = new Uint8Array(32).fill(0x5a);
  const a = await hkdfLoginSplit(master);
  const b = await hkdfLoginSplit(master);
  assert.deepEqual([...a.loginVerifier], [...b.loginVerifier]);
  assert.deepEqual([...a.mkWrapKey], [...b.mkWrapKey]);
});

test("hkdfLoginSplit: 異なる master は異なる出力", async () => {
  const a = await hkdfLoginSplit(new Uint8Array(32).fill(1));
  const b = await hkdfLoginSplit(new Uint8Array(32).fill(2));
  assert.notDeepEqual([...a.loginVerifier], [...b.loginVerifier]);
  assert.notDeepEqual([...a.mkWrapKey], [...b.mkWrapKey]);
});

test("hkdfLoginSplit: master 長 != 32 は reject", async () => {
  await assert.rejects(
    () => hkdfLoginSplit(new Uint8Array(16)),
    /hkdf ikm must be Uint8Array of 32 bytes/,
  );
});

// ------------------------------------------------------------------
// deriveLoginMaterial: 合成 + master ゼロ化 (stub impl)
// ------------------------------------------------------------------

test("deriveLoginMaterial: stub impl で login_verifier / mk_wrap_key を派生", async () => {
  const recorder = [];
  const salt = new Uint8Array(16).fill(3);
  const { loginVerifier, mkWrapKey } = await deriveLoginMaterial(
    "correct horse battery staple",
    salt,
    { impl: makeStubImpl({ recorder }) },
  );
  assert.equal(loginVerifier.byteLength, 32);
  assert.equal(mkWrapKey.byteLength, 32);
  assert.notDeepEqual([...loginVerifier], [...mkWrapKey]);
  // Argon2id 既定パラメータが使われている
  assert.equal(recorder[0].memorySize, 65536);
  assert.equal(recorder[0].iterations, 3);
  assert.equal(recorder[0].hashLength, 32);
});

test("deriveLoginMaterial: 同入力は決定的、異なるパスワード/salt は異なる出力", async () => {
  const impl = makeStubImpl();
  const salt = new Uint8Array(16).fill(9);
  const a = await deriveLoginMaterial("pw-A", salt, { impl });
  const a2 = await deriveLoginMaterial("pw-A", salt, { impl });
  const b = await deriveLoginMaterial("pw-B", salt, { impl });
  const c = await deriveLoginMaterial("pw-A", new Uint8Array(16).fill(8), { impl });
  assert.deepEqual([...a.loginVerifier], [...a2.loginVerifier]);
  assert.notDeepEqual([...a.loginVerifier], [...b.loginVerifier]);
  assert.notDeepEqual([...a.loginVerifier], [...c.loginVerifier]);
});

test("deriveLoginMaterial: 派生後に master (Argon2id 出力) をゼロ化する", async () => {
  // stub が返した Uint8Array (= master) の同一参照を捕捉し、関数返却後に
  // 全バイトが 0 になっていることを確認する。
  let masterRef = null;
  const impl = async (opts) => {
    const out = new Uint8Array(32).fill(0xab);
    masterRef = out; // deriveKeyFromPassphrase はこの参照をそのまま master として使う
    return out;
  };
  await deriveLoginMaterial("pw", new Uint8Array(16), { impl });
  assert.ok(masterRef, "stub should have produced a master buffer");
  assert.deepEqual([...masterRef], new Array(32).fill(0), "master must be zeroized");
});

test("deriveLoginMaterial: 空パスワードは reject (argon2.js 検証に委譲)", async () => {
  await assert.rejects(
    () => deriveLoginMaterial("", new Uint8Array(16), { impl: makeStubImpl() }),
    /passphrase must be non-empty string/,
  );
});

test("deriveLoginMaterial: salt 長 != 16 は reject", async () => {
  await assert.rejects(
    () => deriveLoginMaterial("pw", new Uint8Array(8), { impl: makeStubImpl() }),
    /salt must be Uint8Array of 16 bytes/,
  );
});

// ------------------------------------------------------------------
// 実 Argon2id (vendor hash-wasm) による end-to-end golden vector
//   ~1-2 秒。password+salt → master → login_verifier/mk_wrap_key の完全な契約。
// ------------------------------------------------------------------

test("deriveLoginMaterial: 実 Argon2id の end-to-end golden vector", async () => {
  const vendor = await import(VENDOR_URL.href);
  globalThis.hashwasm = vendor.default ?? vendor;
  setArgon2idImpl(null); // 自動解決 (globalThis.hashwasm) に戻す
  try {
    const salt = new Uint8Array(16).fill(0x42);
    const { loginVerifier, mkWrapKey } = await deriveLoginMaterial(
      "correct horse battery staple",
      salt,
    );
    assert.equal(
      hex(loginVerifier),
      "2b0bccdc18689166e0366e2e2988b6edc5076d5641468a5e2c27c401f42063fb",
    );
    assert.equal(
      hex(mkWrapKey),
      "4c5e2b82b589a378571ca6a321396b53a2df9979b5ae117270b948e3dc6871e3",
    );
  } finally {
    setArgon2idImpl(null);
  }
});
