// crypto/record.js (Phase E3 record-level 暗号化 helper) の単体テスト。
//
// SharedCryptoClient (実際の AES-GCM) は Node で worker が動かないのでモック。
// AAD バイト列のフォーマット (§12.2 設計書) と encrypt/decrypt round-trip
// (mock 経由) を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const REC = new URL(
  "../../../app/static/js/crypto/record.js",
  import.meta.url,
);
const { uint64BE, buildAAD, encryptRecord, decryptRecord } = await import(
  REC.href
);


// ============ uint64BE ============

test("uint64BE: 0 は 8B 全 0", () => {
  assert.deepEqual(uint64BE(0), new Uint8Array(8));
});

test("uint64BE: 1 は 0x00 00 00 00 00 00 00 01", () => {
  assert.deepEqual(
    uint64BE(1),
    new Uint8Array([0, 0, 0, 0, 0, 0, 0, 1]),
  );
});

test("uint64BE: 大きい値 (BigInt)", () => {
  // 0x12_34_56_78_9a_bc_de_f0
  const v = 0x123456789abcdef0n;
  assert.deepEqual(
    uint64BE(v),
    new Uint8Array([0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xf0]),
  );
});

test("uint64BE: 53bit 超え Number は安全に BigInt 化", () => {
  // 2^53 (Number.MAX_SAFE_INTEGER + 1) — 通常の Number → BigInt 変換が精度落とすが
  // 引数を BigInt で渡せば安全
  const v = 1n << 53n;
  const expected = new Uint8Array([0, 0x20, 0, 0, 0, 0, 0, 0]);
  assert.deepEqual(uint64BE(v), expected);
});

test("uint64BE: 負数で throw", () => {
  assert.throws(() => uint64BE(-1), /out of range/);
});

test("uint64BE: 53bit 超え Number は guard で throw (精度ロス防止)", () => {
  // Number.MAX_SAFE_INTEGER + 1 = 2^53 (Number 上は安全範囲外)
  assert.throws(
    () => uint64BE(Number.MAX_SAFE_INTEGER + 1),
    /precision loss.*pass BigInt/,
  );
});


// ============ buildAAD ============
// Option B: je / jel / me は user_id のみ (entry_id 等を含めない)。
// bcb は (year, period) を含む 1 個。

test("buildAAD: je (journal_entries) — user_id のみ", () => {
  const aad = buildAAD("je", 1);
  // b"je\0" + uint64_be(1)
  const expected = new Uint8Array([
    0x6a, 0x65, 0x00,            // "je\0"
    0, 0, 0, 0, 0, 0, 0, 1,      // user_id=1
  ]);
  assert.deepEqual(aad, expected);
});

test("buildAAD: jel (journal_entry_lines) — user_id のみ", () => {
  const aad = buildAAD("jel", 2);
  const expected = new Uint8Array([
    0x6a, 0x65, 0x6c, 0x00,      // "jel\0"
    0, 0, 0, 0, 0, 0, 0, 2,      // user_id=2
  ]);
  assert.deepEqual(aad, expected);
});

test("buildAAD: me (medical_expenses) — user_id のみ", () => {
  const aad = buildAAD("me", 1);
  const expected = new Uint8Array([
    0x6d, 0x65, 0x00,            // "me\0"
    0, 0, 0, 0, 0, 0, 0, 1,
  ]);
  assert.deepEqual(aad, expected);
});

test("buildAAD: bcb (balance_cache_blobs) — user_id + period_key (year*100+period)", () => {
  const aad = buildAAD("bcb", 1, 202605);
  const expected = new Uint8Array([
    0x62, 0x63, 0x62, 0x00,      // "bcb\0"
    0, 0, 0, 0, 0, 0, 0, 1,      // user_id=1
    0x00,
    0, 0, 0, 0, 0, 0x03, 0x17, 0x6d, // 202605 = 0x3_17_6d
  ]);
  assert.deepEqual(aad, expected);
});

test("buildAAD: 未対応 tableType で throw", () => {
  assert.throws(() => buildAAD("evil", 1, 1), /unsupported tableType/);
});

test("buildAAD: AAD が user_id 毎に異なる (すり替え検知の前提)", () => {
  const a = buildAAD("je", 1);
  const b = buildAAD("je", 2);
  assert.notDeepEqual(a, b);
});

test("buildAAD: 同一ユーザの entry-to-entry swap は検知しない (Option B トレードオフ)", () => {
  // Option B では entry_id を AAD に含めないので、同一 user_id の異なる
  // entry 間で AAD が同じ。サーバ侵害時の swap 攻撃は検知できないが、
  // dual-read 撤去で「サーバが平文を見ない」を優先する設計判断。
  const a = buildAAD("je", 1);
  const b = buildAAD("je", 1);
  assert.deepEqual(a, b);
});

test("buildAAD: tableType prefix が異テーブル間で衝突しない", () => {
  // 同じ user_id でもテーブル種別が違えば AAD が違う
  const je = buildAAD("je", 1);
  const jel = buildAAD("jel", 1);
  const me = buildAAD("me", 1);
  assert.notDeepEqual(je, jel);
  assert.notDeepEqual(je, me);
  assert.notDeepEqual(jel, me);
});

test("buildAAD: tableType ごとの ids 個数を厳密検証", () => {
  // je / jel / me は 0 個 (user_id のみ)
  assert.throws(() => buildAAD("je", 1, 100), /expects 0 id\(s\), got 1/);
  assert.throws(() => buildAAD("jel", 1, 100, 200), /expects 0 id\(s\), got 2/);
  assert.throws(() => buildAAD("me", 1, 42), /expects 0 id\(s\), got 1/);
  // bcb は 1 個必要 (year*100+period)
  assert.throws(() => buildAAD("bcb", 1), /expects 1 id\(s\), got 0/);
  assert.throws(() => buildAAD("bcb", 1, 202605, 999), /expects 1 id\(s\), got 2/);
});


// ============ encryptRecord / decryptRecord (mock client) ============

function makeMockClient() {
  // 簡単な mock: encrypt 時に plaintext と aad を WeakMap で紐付け、
  // decrypt 時に aad が一致しなければ throw (実 GCM の挙動を模す)。
  const aadStore = new Map();  // key = ciphertext (Uint8Array reference) string

  function _aadKey(bytes) {
    // base64 of first 32 bytes (テスト範囲で衝突しない簡易キー)
    return Array.from(bytes.slice(0, 32)).join(",");
  }

  return {
    async encrypt(plaintext, aad) {
      const iv = new Uint8Array(12);
      crypto.getRandomValues(iv);
      // ciphertext = plaintext + 16B tag (実 GCM 模倣)
      const ciphertext = new Uint8Array(plaintext.length + 16);
      ciphertext.set(plaintext, 0);
      crypto.getRandomValues(ciphertext.subarray(plaintext.length));
      const aadView = aad ? new Uint8Array(aad) : new Uint8Array();
      // ciphertext の内容を key として AAD を保管
      aadStore.set(_aadKey(ciphertext), aadView);
      return { ciphertext, iv };
    },
    async decrypt(ciphertext, iv, aad) {
      const expected = aadStore.get(_aadKey(ciphertext));
      const actual = aad ? new Uint8Array(aad) : new Uint8Array();
      if (!expected || expected.length !== actual.length) {
        throw new Error("decrypt: GCM tag check failed (AAD mismatch)");
      }
      for (let i = 0; i < expected.length; i++) {
        if (expected[i] !== actual[i]) {
          throw new Error("decrypt: GCM tag check failed (AAD mismatch)");
        }
      }
      const plaintextLen = ciphertext.length - 16;
      return { plaintext: ciphertext.slice(0, plaintextLen) };
    },
  };
}

test("encryptRecord/decryptRecord: round-trip", async () => {
  const client = makeMockClient();
  const record = {
    v: 1, date: "2026-05-22", description: "スーパー",
    source: "cashbook", batch_id: null, fiscal_period: 5,
  };
  const aad = buildAAD("je", 1);
  const { blob, iv } = await encryptRecord(client, record, aad);
  assert.ok(blob instanceof Uint8Array);
  assert.equal(iv.length, 12);
  const decoded = await decryptRecord(client, blob, iv, aad);
  assert.deepEqual(decoded, record);
});

test("decryptRecord: AAD すり替えで throw (user_id 違い)", async () => {
  const client = makeMockClient();
  const record = { v: 1, x: "secret" };
  const aadCorrect = buildAAD("je", 1);
  const aadEvil = buildAAD("je", 2);  // 別ユーザー
  const { blob, iv } = await encryptRecord(client, record, aadCorrect);
  await assert.rejects(
    () => decryptRecord(client, blob, iv, aadEvil),
    /AAD mismatch/,
  );
});

test("decryptRecord: tableType すり替えで throw", async () => {
  const client = makeMockClient();
  const aadCorrect = buildAAD("je", 1);
  const aadEvil = buildAAD("me", 1);  // 別テーブル
  const { blob, iv } = await encryptRecord(client, { x: 1 }, aadCorrect);
  await assert.rejects(
    () => decryptRecord(client, blob, iv, aadEvil),
    /AAD mismatch/,
  );
});

test("encryptRecord: client なしで throw", async () => {
  await assert.rejects(
    () => encryptRecord(null, { x: 1 }, new Uint8Array()),
    /client.*is required/,
  );
});

test("encryptRecord: aad が Uint8Array でないと throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => encryptRecord(client, { x: 1 }, "not bytes"),
    /aad must be a Uint8Array/,
  );
});
