// vendor 同梱 hash-wasm + argon2.js の統合テスト。
//
// 通常の argon2 テスト (test_argon2.mjs) は impl DI で stub を使うため、
// 実 Argon2id が正しく動作するかは検証できていない。本テストは
// `app/static/js/vendor/hash-wasm-4.12.0.esm.min.js` を直接 import し、
// 実際の Argon2id 派生が決定的で正しい長さを返すことを保証する。
//
// 実行コスト: Argon2id 64MiB / iter=3 は ~1-2 秒。Node テストの中では
// 最も重いが、CI で 1 回確認するだけで「vendor 更新時にバージョン互換が
// 取れているか」が見える。

import { test } from "node:test";
import assert from "node:assert/strict";

const ARGON_URL = new URL(
  "../../../app/static/js/crypto/argon2.js",
  import.meta.url,
);
const VENDOR_URL = new URL(
  "../../../app/static/js/vendor/hash-wasm-4.12.0.esm.min.js",
  import.meta.url,
);

const argonModule = await import(ARGON_URL.href);
const { deriveKeyFromPassphrase, ARGON2ID_DEFAULTS, setArgon2idImpl } =
  argonModule;

// vendor を直接読み込み、グローバルに expose (resolveBrowserImpl が拾う)
const hashWasm = await import(VENDOR_URL.href);
globalThis.hashwasm = hashWasm.default ?? hashWasm;


test("vendor hash-wasm の argon2id が関数として読み込める", () => {
  assert.equal(
    typeof globalThis.hashwasm.argon2id,
    "function",
    "globalThis.hashwasm.argon2id should be a function",
  );
});


test("実 Argon2id で deriveKeyFromPassphrase が 32B を返す (時間短縮パラメータ)", async () => {
  // 標準パラメータ (64MiB / iter=3) は Node テストで ~1.5s かかるため、
  // 統合確認には軽量パラメータ (4MiB / iter=1) を使う。
  // 実 Argon2id が呼ばれていることの確認が目的で、KDF 強度ではない。
  setArgon2idImpl(null); // resolveBrowserImpl 経由で vendor を解決
  const salt = new Uint8Array(16).fill(0x42);
  const derived = await deriveKeyFromPassphrase("correct horse", salt, {
    params: { memorySize: 4096, iterations: 1, parallelism: 1, hashLength: 32 },
  });
  assert.equal(derived.byteLength, 32);
  // 全ゼロでないこと (実際に派生されている)
  const nonZero = Array.from(derived).some((b) => b !== 0);
  assert.equal(nonZero, true);
});


test("実 Argon2id は同じ入力で決定的", async () => {
  setArgon2idImpl(null);
  const salt = new Uint8Array(16).fill(0x42);
  const params = { memorySize: 4096, iterations: 1, parallelism: 1, hashLength: 32 };
  const k1 = await deriveKeyFromPassphrase("pw", salt, { params });
  const k2 = await deriveKeyFromPassphrase("pw", salt, { params });
  assert.deepEqual([...k1], [...k2]);
});


test("実 Argon2id は NFKD 正規化と組み合わせて期待通り動く", async () => {
  // 合成済み é (U+00E9) と分解 é (e + U+0301) で同じ derived_key
  // → vendor の argon2id が UTF-8 入力を受け付け、normalizePassphraseBytes
  //   の出力 (NFKD UTF-8 bytes) が正しく処理されることを確認
  //
  // 重要: 文字列リテラルを直接書くとエディタ/Editツールが NFC に正規化
  // してしまい両変数が同じ NFC バイト列になる tautology (PR #146 review 1
  // 指摘)。Unicode escape を使って実バイトが NFC / NFD で異なることを保証。
  setArgon2idImpl(null);
  const salt = new Uint8Array(16).fill(0x33);
  const params = { memorySize: 4096, iterations: 1, parallelism: 1, hashLength: 32 };
  const composed = "café";       // NFC: c a f é (U+00E9 単一)
  const decomposed = "café";    // NFD: c a f e + 合成アクセント (U+0301)
  // バイト列が異なることを実行時にも確認 (将来の変更で再 tautology を防ぐ)
  assert.notDeepEqual(
    [...new TextEncoder().encode(composed)],
    [...new TextEncoder().encode(decomposed)],
    "NFC composed と NFD decomposed の UTF-8 バイト列は異なるはず",
  );
  const k1 = await deriveKeyFromPassphrase(composed, salt, { params });
  const k2 = await deriveKeyFromPassphrase(decomposed, salt, { params });
  // NFKD 正規化により最終的に同じ derived_key になる
  assert.deepEqual([...k1], [...k2]);
});


test("ARGON2ID_DEFAULTS の memorySize は仕様値 64 MiB と一致", () => {
  // vendor 更新時にパラメータ既定値が変わっていないか確認
  assert.equal(ARGON2ID_DEFAULTS.memorySize, 65536);
  assert.equal(ARGON2ID_DEFAULTS.iterations, 3);
  assert.equal(ARGON2ID_DEFAULTS.parallelism, 1);
  assert.equal(ARGON2ID_DEFAULTS.hashLength, 32);
});
