// ログインパスワード由来 MK の KDF プリミティブ (PR-1, #385)。
// 設計書 docs/v5-e2ee/login-derived-mk.md §2 / §3.2 / §3.5。
//
// ログインパスワードを唯一の常用鍵とし、1 本の slow KDF (Argon2id) を回した後、
// 用途ごとに HKDF で分離する (HKDF split):
//
//   master         = Argon2id(login_password, login_salt)        // クライアント内のみ
//   login_verifier = HKDF-SHA256(master, info="iikanji-login-v1")   // ← サーバへ送り照合
//   mk_wrap_key    = HKDF-SHA256(master, info="iikanji-mk-wrap-v1")  // ← サーバに送らない。MK を unwrap
//
// HKDF は `HKDF-SHA256(ikm=master, salt=zero(32B), info=<上記文字列>, L=32)`。
// salt=zero(32B) の根拠: master は Argon2id 出力で高エントロピーなため RFC 5869 §2.2 の
// ゼロ salt 使用条件を満たす (既存 bip39.js / webauthn_prf.js と同じ流儀)。
//
// info 文字列は UTF-8 bytes として扱う (Python b"...", JS TextEncoder)。client-py / TUI と
// byte 互換が要るため、文字列・エンコードを実装間で厳守する (PR-5 の golden vector 契約)。
//
// セキュリティ: `master` / `mk_wrap_key` は鍵素材。`master` は login_verifier / mk_wrap_key を
// 派生したら即座にゼロ化する (本モジュールが deriveLoginMaterial 内で責任を持つ)。`mk_wrap_key`
// は呼び出し側が MK の wrap/unwrap に使った後にゼロ化する (設計書 §3.2 step4)。

import { deriveKeyFromPassphrase } from "./argon2.js";

// HKDF info (ドメイン分離)。設計書 §2「HKDF info 一覧」と一致させる。短縮形を使わない。
export const LOGIN_VERIFIER_INFO = "iikanji-login-v1";
export const MK_WRAP_KEY_INFO = "iikanji-mk-wrap-v1";

/**
 * HKDF-SHA256(ikm, salt=zero(32B), info, L=32) で 32B を派生する。
 *
 * @param {Uint8Array} ikm   入力鍵素材 (master, 32B 想定)
 * @param {string} info      ドメイン分離文字列 (UTF-8 bytes として扱う)
 * @returns {Promise<Uint8Array>} 32B derived bytes
 */
async function hkdfExpand(ikm, info) {
  if (!(ikm instanceof Uint8Array) || ikm.byteLength !== 32) {
    throw new Error("hkdf ikm must be Uint8Array of 32 bytes");
  }
  const salt = new Uint8Array(32); // all-zero (RFC 5869 §2.2: 高エントロピー IKM ならゼロ salt 可)
  const infoBytes = new TextEncoder().encode(info);
  const key = await crypto.subtle.importKey(
    "raw", ikm, { name: "HKDF" }, false, ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt, info: infoBytes },
    key,
    256,
  );
  return new Uint8Array(bits);
}

/**
 * master を login_verifier / mk_wrap_key に HKDF split する (純粋関数)。
 *
 * master を所与とする決定的変換なので、golden vector (固定 master → 固定出力) の
 * 契約点になる。client-py / TUI はこの関数と同じ HKDF 仕様を byte 互換で実装する。
 *
 * @param {Uint8Array} master  Argon2id 出力 (32B)
 * @returns {Promise<{loginVerifier: Uint8Array, mkWrapKey: Uint8Array}>}
 */
export async function hkdfLoginSplit(master) {
  const loginVerifier = await hkdfExpand(master, LOGIN_VERIFIER_INFO);
  const mkWrapKey = await hkdfExpand(master, MK_WRAP_KEY_INFO);
  return { loginVerifier, mkWrapKey };
}

/**
 * ログインパスワード + salt から login_verifier / mk_wrap_key を派生する。
 *
 * `master = Argon2id(password, salt)` を求め、HKDF split したのち **master を即座に
 * ゼロ化**する。返り値の `mkWrapKey` は呼び出し側が wrap/unwrap 後にゼロ化する責任を持つ。
 *
 * @param {string} password    ユーザーのログインパスワード (argon2.js が NFKD 正規化)
 * @param {Uint8Array} salt     login_salt (16B)
 * @param {Object} [opts]
 * @param {Object} [opts.params]  Argon2id パラメータ (省略時 argon2.js の既定値)
 * @param {Function} [opts.impl]  argon2id 実装 (テスト DI 用)
 * @returns {Promise<{loginVerifier: Uint8Array, mkWrapKey: Uint8Array}>}
 */
export async function deriveLoginMaterial(password, salt, opts = {}) {
  const master = await deriveKeyFromPassphrase(password, salt, opts);
  try {
    return await hkdfLoginSplit(master);
  } finally {
    // master はこれ以降不要。鍵素材なので即座にゼロ化する (設計書 §3.2 step4)。
    master.fill(0);
  }
}
