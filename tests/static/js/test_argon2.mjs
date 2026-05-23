// Argon2id ラッパーの Node 単体テスト。
//
// 実 Argon2id (hash-wasm) を依存に持ちたくないテスト環境用に、impl DI を使った
// 動作検証を中心にする。hash-wasm 統合テストは別途 (PR-F2 統合 or 手動検証)。

import { test } from "node:test";
import assert from "node:assert/strict";

const URL_ = new URL(
  "../../../app/static/js/crypto/argon2.js",
  import.meta.url,
);
const {
  ARGON2ID_DEFAULTS,
  deriveKeyFromPassphrase,
  generateSalt,
  normalizePassphraseBytes,
  setArgon2idImpl,
} = await import(URL_.href);


function makeStubImpl({ expectedHashLength = 32, recorder } = {}) {
  return async (opts) => {
    if (recorder) recorder.push(opts);
    // 決定的: SHA-256(password || salt) の先頭 hashLength バイトを返す (テスト用)
    const enc = new TextEncoder();
    const pwBytes =
      typeof opts.password === "string" ? enc.encode(opts.password) : opts.password;
    const combined = new Uint8Array(pwBytes.byteLength + opts.salt.byteLength);
    combined.set(pwBytes, 0);
    combined.set(opts.salt, pwBytes.byteLength);
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", combined));
    return hash.slice(0, opts.hashLength ?? expectedHashLength);
  };
}


test("ARGON2ID_DEFAULTS は設計書既定値 (64MiB / 3 / 1 / 32)", () => {
  assert.equal(ARGON2ID_DEFAULTS.memorySize, 65536);
  assert.equal(ARGON2ID_DEFAULTS.iterations, 3);
  assert.equal(ARGON2ID_DEFAULTS.parallelism, 1);
  assert.equal(ARGON2ID_DEFAULTS.hashLength, 32);
});


test("ARGON2ID_DEFAULTS は frozen (改変不可)", () => {
  assert.throws(() => { ARGON2ID_DEFAULTS.iterations = 1; }, TypeError);
});


test("generateSalt は 16B 乱数を返す + 毎回異なる", () => {
  const a = generateSalt();
  const b = generateSalt();
  assert.equal(a.byteLength, 16);
  assert.equal(b.byteLength, 16);
  assert.notDeepEqual([...a], [...b]);
});


test("deriveKeyFromPassphrase: passphrase 空文字は reject", async () => {
  await assert.rejects(
    () => deriveKeyFromPassphrase("", generateSalt()),
    /passphrase must be non-empty string/,
  );
});


test("deriveKeyFromPassphrase: salt 長 != 16 は reject", async () => {
  await assert.rejects(
    () => deriveKeyFromPassphrase("hello", new Uint8Array(8)),
    /salt must be Uint8Array of 16 bytes/,
  );
});


test("deriveKeyFromPassphrase: impl 未注入かつ window なしは reject", async () => {
  setArgon2idImpl(null);
  await assert.rejects(
    () => deriveKeyFromPassphrase("hello", generateSalt()),
    /argon2id implementation not available/,
  );
});


test("deriveKeyFromPassphrase: impl DI で正常派生", async () => {
  const recorder = [];
  const impl = makeStubImpl({ recorder });
  const salt = generateSalt();
  const k = await deriveKeyFromPassphrase("correct horse battery staple", salt, { impl });
  assert.equal(k.byteLength, 32);
  assert.equal(recorder.length, 1);
  // パラメータが既定値で渡されている
  assert.equal(recorder[0].memorySize, 65536);
  assert.equal(recorder[0].iterations, 3);
  assert.equal(recorder[0].parallelism, 1);
  assert.equal(recorder[0].hashLength, 32);
  assert.equal(recorder[0].outputType, "binary");
  assert.deepEqual([...recorder[0].salt], [...salt]);
});


test("deriveKeyFromPassphrase: 同じ入力で決定的 (stub の挙動を確認)", async () => {
  const impl = makeStubImpl();
  const salt = new Uint8Array(16); // 固定
  const k1 = await deriveKeyFromPassphrase("pw1", salt, { impl });
  const k2 = await deriveKeyFromPassphrase("pw1", salt, { impl });
  assert.deepEqual([...k1], [...k2]);
});


test("deriveKeyFromPassphrase: 異なる passphrase は異なる鍵を返す", async () => {
  const impl = makeStubImpl();
  const salt = new Uint8Array(16);
  const k1 = await deriveKeyFromPassphrase("pw1", salt, { impl });
  const k2 = await deriveKeyFromPassphrase("pw2", salt, { impl });
  assert.notDeepEqual([...k1], [...k2]);
});


test("deriveKeyFromPassphrase: 異なる salt は異なる鍵を返す", async () => {
  const impl = makeStubImpl();
  const k1 = await deriveKeyFromPassphrase("pw", new Uint8Array(16).fill(1), { impl });
  const k2 = await deriveKeyFromPassphrase("pw", new Uint8Array(16).fill(2), { impl });
  assert.notDeepEqual([...k1], [...k2]);
});


test("deriveKeyFromPassphrase: opts.params で kdf_params を上書き", async () => {
  const recorder = [];
  const impl = makeStubImpl({ recorder });
  await deriveKeyFromPassphrase("pw", generateSalt(), {
    impl,
    params: { memorySize: 32768, iterations: 2 },
  });
  // 既定値とマージされた結果
  assert.equal(recorder[0].memorySize, 32768);
  assert.equal(recorder[0].iterations, 2);
  assert.equal(recorder[0].parallelism, 1); // 既定維持
  assert.equal(recorder[0].hashLength, 32); // 既定維持
});


test("deriveKeyFromPassphrase: impl が異常長を返したら reject", async () => {
  const badImpl = async () => new Uint8Array(16); // hashLength != 32
  await assert.rejects(
    () => deriveKeyFromPassphrase("pw", generateSalt(), { impl: badImpl }),
    /argon2id returned unexpected output/,
  );
});


test("normalizePassphraseBytes: NFKD 正規化 (合成 vs 分解アクセント)", () => {
  // "café" の é = U+00E9 (合成) と U+0065+U+0301 (分解) は同じ NFKD バイト列
  const composed = "café";       // é = U+00E9 (NFC)
  const decomposed = "café";    // e + combining acute (NFD)
  const a = normalizePassphraseBytes(composed);
  const b = normalizePassphraseBytes(decomposed);
  assert.deepEqual([...a], [...b]);
  // ASCII 部分は通常通り
  assert.equal(a[0], "c".charCodeAt(0));
});


test("normalizePassphraseBytes: NFKD は互換文字 (全角/半角) も正規化する", () => {
  // 全角数字 "１" U+FF11 は NFKD で "1" U+0031 (半角) に分解される
  const fullwidth = "１"; // ＝１
  const halfwidth = "1";
  const a = normalizePassphraseBytes(fullwidth);
  const b = normalizePassphraseBytes(halfwidth);
  assert.deepEqual([...a], [...b]);
});


test("normalizePassphraseBytes: 空文字は reject", () => {
  assert.throws(() => normalizePassphraseBytes(""), /non-empty string/);
});


test("deriveKeyFromPassphrase: NFKD 等価な passphrase は同じ derived_key", async () => {
  const recorder = [];
  const impl = makeStubImpl({ recorder });
  const salt = new Uint8Array(16);
  const composed = "passwörd";    // ö U+00F6
  const decomposed = "passwörd"; // o + combining diaeresis
  const k1 = await deriveKeyFromPassphrase(composed, salt, { impl });
  const k2 = await deriveKeyFromPassphrase(decomposed, salt, { impl });
  assert.deepEqual([...k1], [...k2]);
  // impl に渡される password は Uint8Array (NFKD 正規化済み)
  assert.ok(recorder[0].password instanceof Uint8Array);
  assert.deepEqual([...recorder[0].password], [...recorder[1].password]);
});


test("setArgon2idImpl: グローバル impl 設定で opts.impl 省略可", async () => {
  const recorder = [];
  setArgon2idImpl(makeStubImpl({ recorder }));
  try {
    const k = await deriveKeyFromPassphrase("pw", generateSalt());
    assert.equal(k.byteLength, 32);
    assert.equal(recorder.length, 1);
  } finally {
    setArgon2idImpl(null);
  }
});
