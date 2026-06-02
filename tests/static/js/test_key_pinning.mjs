// TOFU 公開鍵 pinning モジュールのテスト (E5 #112 / §14.4)。
// node --test (Node 22+ WebCrypto) で実行。IndexedDB には依存せず、純粋ロジックと
// in-memory ストアで evaluatePin / pinKey / unpinKey のフローを検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  base32Encode,
  bytesToHex,
  computeFingerprint,
  classifyPin,
  evaluatePin,
  pinKey,
  unpinKey,
  createMemoryPinStore,
} from "../../../app/static/js/crypto/key_pinning.js";

test("base32Encode は RFC 4648 のテストベクタと一致する", () => {
  const enc = (s) => base32Encode(new TextEncoder().encode(s));
  // RFC 4648 §10 (パディング無し版)
  assert.equal(enc(""), "");
  assert.equal(enc("f"), "MY");
  assert.equal(enc("fo"), "MZXQ");
  assert.equal(enc("foo"), "MZXW6");
  assert.equal(enc("foob"), "MZXW6YQ");
  assert.equal(enc("fooba"), "MZXW6YTB");
  assert.equal(enc("foobar"), "MZXW6YTBOI");
});

test("bytesToHex は固定長 2 桁 hex を返す", () => {
  assert.equal(bytesToHex(new Uint8Array([0, 1, 15, 16, 255])), "00010f10ff");
  assert.equal(bytesToHex(new Uint8Array([])), "");
});

test("computeFingerprint は決定的で iikanji-<ROLE>- 形式のラベルを返す", async () => {
  const pub = new Uint8Array(32).fill(7);
  const a = await computeFingerprint(pub);
  const b = await computeFingerprint(pub);
  assert.equal(a.hashHex, b.hashHex);
  assert.equal(a.label, b.label);
  assert.equal(a.hashHex.length, 64); // SHA-256 全 32 バイトの hex
  assert.match(a.label, /^iikanji-AUDITOR-[A-Z2-7]{4}(-[A-Z2-7]{4})+$/);
  // 先頭 20 バイト (160 bit) → 32 base32 文字 → 8 グループ
  const groups = a.label.replace("iikanji-AUDITOR-", "").split("-");
  assert.equal(groups.length, 8);
});

test("computeFingerprint は role を差し替えられる", async () => {
  const pub = new Uint8Array(32).fill(1);
  const a = await computeFingerprint(pub, "OWNER");
  assert.match(a.label, /^iikanji-OWNER-/);
});

test("異なる公開鍵は異なる fingerprint", async () => {
  const a = await computeFingerprint(new Uint8Array(32).fill(1));
  const b = await computeFingerprint(new Uint8Array(32).fill(2));
  assert.notEqual(a.hashHex, b.hashHex);
  assert.notEqual(a.label, b.label);
});

test("classifyPin は一致/不一致を判定する", () => {
  assert.equal(classifyPin("abcd", "abcd"), "match");
  assert.equal(classifyPin("abcd", "abce"), "mismatch");
});

test("evaluatePin: 未 pin → unpinned、pin 後 → match", async () => {
  const store = createMemoryPinStore();
  const pub = new Uint8Array(32).fill(9);

  const before = await evaluatePin(store, 42, pub);
  assert.equal(before.status, "unpinned");
  assert.equal(before.pinnedAt, null);

  await pinKey(store, 42, before.hashHex, "2026-06-02T00:00:00.000Z");

  const after = await evaluatePin(store, 42, pub);
  assert.equal(after.status, "match");
  assert.equal(after.pinnedAt, "2026-06-02T00:00:00.000Z");
  assert.equal(after.hashHex, before.hashHex);
});

test("evaluatePin: 公開鍵がすり替わると mismatch", async () => {
  const store = createMemoryPinStore();
  const original = new Uint8Array(32).fill(3);
  const ev = await evaluatePin(store, 7, original);
  await pinKey(store, 7, ev.hashHex, "2026-06-02T00:00:00.000Z");

  const swapped = new Uint8Array(32).fill(4);
  const after = await evaluatePin(store, 7, swapped);
  assert.equal(after.status, "mismatch");
  // ラベル/ハッシュは「現在の (すり替わった) 鍵」のもの
  const swappedFp = await computeFingerprint(swapped);
  assert.equal(after.hashHex, swappedFp.hashHex);
});

test("unpinKey で pinning が消え、再び unpinned になる", async () => {
  const store = createMemoryPinStore();
  const pub = new Uint8Array(32).fill(5);
  const ev = await evaluatePin(store, 100, pub);
  await pinKey(store, 100, ev.hashHex, "2026-06-02T00:00:00.000Z");
  assert.equal((await evaluatePin(store, 100, pub)).status, "match");

  await unpinKey(store, 100);
  assert.equal((await evaluatePin(store, 100, pub)).status, "unpinned");
});

test("ストアは peer_user_id ごとに独立", async () => {
  const store = createMemoryPinStore();
  const pubA = new Uint8Array(32).fill(1);
  const pubB = new Uint8Array(32).fill(2);
  const evA = await evaluatePin(store, 1, pubA);
  await pinKey(store, 1, evA.hashHex, "2026-06-02T00:00:00.000Z");

  // peer 2 は未 pin のまま
  assert.equal((await evaluatePin(store, 2, pubB)).status, "unpinned");
  assert.equal((await evaluatePin(store, 1, pubA)).status, "match");
});
