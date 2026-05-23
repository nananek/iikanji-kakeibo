// パスフレーズ → derived_key 派生 (Argon2id) のラッパー (E1 PR-F1)。
// 設計書 §2 / §10 (kdf_params 仕様)。
//
// パラメータ (設計書既定):
//   memory      = 64 MiB (65536 KiB)
//   iterations  = 3
//   parallelism = 1
//   hashLength  = 32 (= derived_key 長)
//
// 実装方針:
// - Argon2id は WebCrypto に標準実装がないため hash-wasm (WASM ビルド) を使う
// - ブラウザ: <script type="module"> で hash-wasm の ESM を読み込み、
//   `window.hashwasm.argon2id` を expose する想定
// - Node テスト: `argon2idImpl` を DI で差し込む (実関数 or mock)
//
// セキュリティ:
// - 戻り値 derived_key (32B) は wrap/unwrap 後に呼び出し側で必ずゼロ埋め
// - パスフレーズ文字列は JS string で GC 制御不能 (ゼロ埋め不可)。これは
//   WebCrypto API の制約で受容する。ただし Uint8Array に変換後はゼロ埋め可能

// 設計書既定の Argon2id パラメータ。インスタンス間で固定値として共有する。
export const ARGON2ID_DEFAULTS = Object.freeze({
  memorySize: 65536,   // KiB = 64 MiB
  iterations: 3,
  parallelism: 1,
  hashLength: 32,
});

/**
 * hash-wasm の `argon2id` 関数の signature:
 *   argon2id({ password, salt, parallelism, iterations, memorySize, hashLength, outputType })
 *     → Promise<Uint8Array | string>
 *
 * password / salt は Uint8Array, string どちらも可。本ラッパーでは Uint8Array に統一。
 *
 * @typedef {Object} Argon2idOptions
 * @property {Uint8Array|string} password
 * @property {Uint8Array} salt
 * @property {number} parallelism
 * @property {number} iterations
 * @property {number} memorySize
 * @property {number} hashLength
 * @property {"binary"|"encoded"|"hex"} outputType
 */

let _defaultImpl = null;

/** ブラウザでの自動解決を試みる。`window.hashwasm.argon2id` を探す。 */
function resolveBrowserImpl() {
  if (
    typeof globalThis !== "undefined" &&
    globalThis.hashwasm &&
    typeof globalThis.hashwasm.argon2id === "function"
  ) {
    return globalThis.hashwasm.argon2id;
  }
  if (
    typeof window !== "undefined" &&
    window.hashwasm &&
    typeof window.hashwasm.argon2id === "function"
  ) {
    return window.hashwasm.argon2id;
  }
  return null;
}

/**
 * テスト用 / 明示的注入用に argon2id 実装を差し替える。
 * `null` を渡すと自動解決 (ブラウザ window.hashwasm) に戻る。
 */
export function setArgon2idImpl(impl) {
  _defaultImpl = impl;
}

/**
 * パスフレーズ + salt から 32B の derived_key を派生する。
 *
 * @param {string} passphrase  ユーザー入力 (UTF-8 として処理)
 * @param {Uint8Array} salt    per-user salt (16B、wrapped_keys.salt)
 * @param {Object} [opts]
 * @param {Object} [opts.params]    Argon2id パラメータ ({memory, iterations, parallelism})
 *                                   省略時は ARGON2ID_DEFAULTS
 * @param {Function} [opts.impl]    argon2id 関数 (テスト DI 用)
 * @returns {Promise<Uint8Array>}   32B derived_key (呼び出し側でゼロ埋め必須)
 */
export async function deriveKeyFromPassphrase(passphrase, salt, opts = {}) {
  if (typeof passphrase !== "string" || passphrase.length === 0) {
    throw new Error("passphrase must be non-empty string");
  }
  if (!(salt instanceof Uint8Array) || salt.byteLength !== 16) {
    throw new Error("salt must be Uint8Array of 16 bytes");
  }
  const impl = opts.impl ?? _defaultImpl ?? resolveBrowserImpl();
  if (typeof impl !== "function") {
    throw new Error(
      "argon2id implementation not available — call setArgon2idImpl() or load hash-wasm in browser",
    );
  }
  const params = { ...ARGON2ID_DEFAULTS, ...(opts.params ?? {}) };
  const result = await impl({
    password: passphrase,
    salt,
    parallelism: params.parallelism,
    iterations: params.iterations,
    memorySize: params.memorySize,
    hashLength: params.hashLength,
    outputType: "binary",
  });
  if (!(result instanceof Uint8Array) || result.byteLength !== params.hashLength) {
    throw new Error(
      `argon2id returned unexpected output: ${
        result?.byteLength ?? typeof result
      } bytes`,
    );
  }
  return result;
}

/**
 * 新規 wrapped_keys.salt 用の per-user salt (16B 乱数) を生成。
 */
export function generateSalt() {
  return crypto.getRandomValues(new Uint8Array(16));
}
