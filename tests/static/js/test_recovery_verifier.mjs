// #385 PR-4b-1: deriveRecoveryVerifier の Node 単体テスト。
//
// 設計書 §3.4.1: 同一シードから別 info で recovery_verifier を独立導出する。
// client-py/TUI と byte 互換させるため golden vector で固定する。

import { test } from "node:test";
import assert from "node:assert/strict";

const BIP39_URL = new URL(
  "../../../app/static/js/crypto/bip39.js",
  import.meta.url,
);
const { deriveRecoveryVerifier, deriveKeyFromMnemonic } = await import(BIP39_URL.href);

function bytesToHex(b) {
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

// 全ゼロ entropy のニーモニック (BIP-39 公式ベクトル)。
const ZERO_MNEMONIC =
  "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art";

// golden vector: HKDF-SHA256(seed_bytes, salt=zero(32), info="iikanji-recovery-login-v1", L=32)。
// client-py/TUI はこの値と byte 一致させること。
const GOLDEN_RECOVERY_VERIFIER =
  "68a6173b2cc1666e6c19e2dfe7315cd0fd3a2ec33688372adad79aedd478eb0c";


test("golden vector: 固定シード → 固定 recovery_verifier (32B)", async () => {
  const rv = await deriveRecoveryVerifier(ZERO_MNEMONIC);
  assert.equal(rv.length, 32);
  assert.equal(bytesToHex(rv), GOLDEN_RECOVERY_VERIFIER);
});


test("ドメイン分離: recovery_verifier は MK unwrap 鍵と別値", async () => {
  const rv = await deriveRecoveryVerifier(ZERO_MNEMONIC);
  const mk = await deriveKeyFromMnemonic(ZERO_MNEMONIC);
  assert.notEqual(bytesToHex(rv), bytesToHex(mk));
});


test("決定的: 同一シードで毎回同じ", async () => {
  const a = await deriveRecoveryVerifier(ZERO_MNEMONIC);
  const b = await deriveRecoveryVerifier(ZERO_MNEMONIC);
  assert.equal(bytesToHex(a), bytesToHex(b));
});


test("正規化共有: 前後空白/連続空白/大文字は deriveKeyFromMnemonic と同じく無視", async () => {
  // deriveKeyFromMnemonic と同一正規化 (trim → toLowerCase → 連続空白畳み) を共有する。
  const messy = `  ${ZERO_MNEMONIC.toUpperCase().replace(/ /g, "   ")}  `;
  const clean = await deriveRecoveryVerifier(ZERO_MNEMONIC);
  const dirty = await deriveRecoveryVerifier(messy);
  assert.equal(bytesToHex(dirty), bytesToHex(clean));
});


test("不正シード (チェックサム不一致) は reject", async () => {
  const bad = ZERO_MNEMONIC.replace(/art$/, "abandon"); // 末尾語のチェックサム崩す
  await assert.rejects(() => deriveRecoveryVerifier(bad));
});
