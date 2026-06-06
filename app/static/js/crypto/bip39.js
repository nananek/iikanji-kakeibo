// BIP-39 24 単語リカバリシード生成 / 検証 / derived_key 派生 (E1 PR-F1)。
// 設計書 §2 リカバリシード / §8 リカバリ / §10.1 wrapped_keys (recovery_seed)。
//
// 仕様 (BIP-39):
// - エントロピー 256 bit (32 バイト) を 24 単語に符号化
// - チェックサム = SHA-256(entropy) の先頭 ENT/32 = 8 bit
// - (entropy + checksum) = 264 bit を 11 bit ずつ 24 グループに分割
// - 各 11 bit (0〜2047) を wordlist でルックアップ
//
// derived_key 派生 (本アプリ独自):
// - 設計書 §10.1: HKDF-SHA256 の入力は **mnemonic UTF-8 バイト列**, salt=zero,
//   info="iikanji-master-key-v1"
// - BIP-39 標準の PBKDF2-HMAC-SHA512(mnemonic, "mnemonic" + passphrase) は使わない
//   (passphrase は別ファクタとして wrapped_keys.method=passphrase で扱うため、
//    recovery_seed では passphrase を絡めない設計)
//
// 注意:
// - mnemonic は string で扱うが、復元処理後は呼び出し側で参照を捨て GC を促す
// - derived_key は Uint8Array 32B、wrap/unwrap 後にゼロ埋め必須

import { BIP39_ENGLISH_WORDLIST } from "./bip39_wordlist_en.js";

const ENTROPY_BITS = 256;
const WORD_COUNT = 24;
const CHECKSUM_BITS = ENTROPY_BITS / 32; // = 8
const TOTAL_BITS = ENTROPY_BITS + CHECKSUM_BITS; // = 264
const BITS_PER_WORD = 11; // 2^11 = 2048

// HKDF info コンテキスト。設計書 §2 / §3 と一致させる (鍵ドメイン分離)。
const HKDF_INFO = "iikanji-master-key-v1";
// #385 PR-4b-1: リカバリシードをフル復旧因子化する verifier の info (設計書 §3.4.1)。
// 同一 seed_bytes から別 info で MK unwrap 鍵 (HKDF_INFO) と独立に導出する。
const RECOVERY_VERIFIER_INFO = "iikanji-recovery-login-v1";

/** wordlist がちょうど 2048 語であることを起動時に確認 (壊れた場合の安全装置)。 */
if (BIP39_ENGLISH_WORDLIST.length !== 2048) {
  throw new Error(
    `BIP39 wordlist length must be 2048, got ${BIP39_ENGLISH_WORDLIST.length}`,
  );
}

// 逆引き Map: 単語 → インデックス。O(1) ルックアップ用 (mnemonicToEntropy で使用)。
// モジュール初期化時に 1 度だけ構築。
const WORD_TO_INDEX = new Map(BIP39_ENGLISH_WORDLIST.map((w, i) => [w, i]));

/**
 * 32B エントロピーをチェックサム付きで 24 単語に符号化。
 * @param {Uint8Array} entropy 32 バイト
 * @returns {Promise<string>}  半角スペース区切りの 24 単語
 */
export async function entropyToMnemonic(entropy) {
  if (!(entropy instanceof Uint8Array) || entropy.byteLength !== 32) {
    throw new Error("entropy must be Uint8Array of 32 bytes");
  }
  // チェックサム = SHA-256(entropy) の先頭 8 bit
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  const checksumByte = hash[0]; // 先頭バイト (8 bit) を使う

  // 264 bit を 11 bit ずつ 24 ワードに分割
  // bits 配列: 0/1 の Boolean ではなく数値処理しやすいよう累積整数で
  const words = [];
  // 264-bit を 32-bit ずつ uint8 配列に展開し、bit cursor で 11-bit ずつ取り出す
  // 単純化のため、bit を 0/1 の string にして 11 文字ずつ parseInt するアプローチ
  let bits = "";
  for (const b of entropy) {
    bits += b.toString(2).padStart(8, "0");
  }
  bits += checksumByte.toString(2).padStart(8, "0");
  if (bits.length !== TOTAL_BITS) {
    throw new Error(`internal: bit length ${bits.length} != ${TOTAL_BITS}`);
  }
  for (let i = 0; i < WORD_COUNT; i++) {
    const slice = bits.slice(i * BITS_PER_WORD, (i + 1) * BITS_PER_WORD);
    const idx = parseInt(slice, 2);
    words.push(BIP39_ENGLISH_WORDLIST[idx]);
  }
  return words.join(" ");
}

/**
 * 24 単語ニーモニックをチェックサム検証してエントロピー 32B に復元。
 * @param {string} mnemonic 24 単語 (空白区切り、大文字小文字無視)
 * @returns {Promise<Uint8Array>} 32 バイトエントロピー
 */
export async function mnemonicToEntropy(mnemonic) {
  if (typeof mnemonic !== "string") {
    throw new Error("mnemonic must be string");
  }
  // BIP-39 は単一空白区切り想定だが、ユーザー入力は連続空白を許容
  const words = mnemonic.trim().toLowerCase().split(/\s+/);
  if (words.length !== WORD_COUNT) {
    throw new Error(`mnemonic must be ${WORD_COUNT} words, got ${words.length}`);
  }
  let bits = "";
  for (const w of words) {
    const idx = WORD_TO_INDEX.get(w);
    if (idx === undefined) {
      throw new Error(`unknown word in mnemonic: "${w}"`);
    }
    bits += idx.toString(2).padStart(BITS_PER_WORD, "0");
  }
  if (bits.length !== TOTAL_BITS) {
    throw new Error(`internal: bit length ${bits.length} != ${TOTAL_BITS}`);
  }
  // entropy 256 bit (32 byte) + checksum 8 bit (1 byte)
  const entropyBits = bits.slice(0, ENTROPY_BITS);
  const checksumBits = bits.slice(ENTROPY_BITS);
  const entropy = new Uint8Array(32);
  for (let i = 0; i < 32; i++) {
    entropy[i] = parseInt(entropyBits.slice(i * 8, (i + 1) * 8), 2);
  }
  // チェックサム検証
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", entropy));
  const expectedChecksumBits = hash[0].toString(2).padStart(8, "0");
  if (checksumBits !== expectedChecksumBits) {
    throw new Error("mnemonic checksum mismatch");
  }
  return entropy;
}

/** 256-bit 乱数エントロピーから新しい 24 単語ニーモニックを生成。 */
export async function generateMnemonic() {
  const entropy = crypto.getRandomValues(new Uint8Array(32));
  try {
    return await entropyToMnemonic(entropy);
  } finally {
    // entropy 自体は呼び出し直後の mnemonic に符号化された後は不要
    entropy.fill(0);
  }
}

/**
 * ニーモニックから derived_key (32B) を派生。
 *
 * 設計書 §10.1 既定:
 *   HKDF-SHA256(
 *     input = mnemonic UTF-8 bytes,
 *     salt  = zero (16B 想定、ここでは empty も等価),
 *     info  = "iikanji-master-key-v1",
 *     L     = 32
 *   )
 *
 * salt は wrapped_keys.salt (method=recovery_seed では NULL 保存) に依存しない
 * 仕様。recover_seed 経由の wrap 結果には salt を持たないため再現性が保たれる。
 *
 * @param {string} mnemonic
 * @returns {Promise<Uint8Array>} 32B derived_key (呼び出し側でゼロ埋め必須)
 */
export async function deriveKeyFromMnemonic(mnemonic) {
  // 入力検証 + チェックサム検証を兼ねて先に entropy 復元
  const entropy = await mnemonicToEntropy(mnemonic);
  // 復元成功した時点でチェックサムは OK。entropy は派生に直接は使わず、
  // HKDF の input は mnemonic UTF-8 バイト列 (設計書記述に忠実) とする
  entropy.fill(0); // 即座にゼロ埋め (派生処理には不要)

  return hkdfFromMnemonic(mnemonic, HKDF_INFO);
}

/**
 * リカバリシードからサーバ照合用 recovery_verifier (32B) を派生する (#385 PR-4b-1)。
 *
 * MK unwrap 鍵 (deriveKeyFromMnemonic, info="iikanji-master-key-v1") と**同一の
 * seed_bytes 正規化**を共有しつつ、info="iikanji-recovery-login-v1" で独立に導出する
 * (設計書 §3.4.1)。同一シードから別 info の 2 値は HKDF の PRF 性で独立。
 *
 * サーバはこれを HMAC(LOGIN_SERVER_SECRET, "recovery-hash"||0x00||recovery_verifier)
 * にして recovery_seed_server_hash に保存・照合する。
 *
 * @param {string} mnemonic  BIP-39 24 語
 * @returns {Promise<Uint8Array>} 32B recovery_verifier (呼び出し側でゼロ埋め必須)
 */
export async function deriveRecoveryVerifier(mnemonic) {
  // verifier 派生でもチェックサム検証は通しておく (不正シードを早期に弾く)。
  const entropy = await mnemonicToEntropy(mnemonic);
  entropy.fill(0);
  return hkdfFromMnemonic(mnemonic, RECOVERY_VERIFIER_INFO);
}

/**
 * seed_bytes 正規化 + HKDF-SHA256(salt=zero(32), info, L=32) の共通実装。
 *
 * 正規化は MK unwrap 鍵と recovery_verifier で**完全一致させる** (どちらも同じ
 * seed_bytes を IKM にする)。client-py/TUI もこの手順と byte 一致させること
 * (設計書 §2 プリミティブ仕様 / §3.4.1)。注: 既存リカバリシードとの後方互換のため
 * trim → toLowerCase → 連続空白畳み の手順を変更しない (英語 BIP-39 語彙では NFKD は
 * no-op なので省略しても byte 一致する)。
 */
async function hkdfFromMnemonic(mnemonic, info) {
  const normalized = mnemonic.trim().toLowerCase().split(/\s+/).join(" ");
  const inputBytes = new TextEncoder().encode(normalized);
  const salt = new Uint8Array(32); // all-zero (HKDF-SHA256 推奨の hashLen=32)
  const infoBytes = new TextEncoder().encode(info);
  try {
    // HKDF: importKey(raw, HKDF) → deriveBits(HKDF, salt, info, L=256 bit)
    const ikm = await crypto.subtle.importKey(
      "raw", inputBytes, { name: "HKDF" }, false, ["deriveBits"],
    );
    const derived = await crypto.subtle.deriveBits(
      {
        name: "HKDF",
        hash: "SHA-256",
        salt,
        info: infoBytes,
      },
      ikm,
      256,
    );
    return new Uint8Array(derived);
  } finally {
    // mnemonic UTF-8 表現も鍵素材相当なので派生後にゼロ埋め
    inputBytes.fill(0);
  }
}
