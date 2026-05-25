// b64encode / b64decode の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const { b64encode, b64decode } = await import(
  new URL("../../../app/static/js/crypto/b64.js", import.meta.url).href
);


test("b64encode: 空配列", () => {
  assert.equal(b64encode(new Uint8Array(0)), "");
});

test("b64encode: ASCII バイト列", () => {
  // "Hi" = 0x48 0x69 → SGk=
  assert.equal(b64encode(new Uint8Array([0x48, 0x69])), "SGk=");
});

test("b64decode: 空文字列", () => {
  assert.deepEqual(b64decode(""), new Uint8Array(0));
});

test("b64decode: ASCII Base64", () => {
  // SGk= → 0x48 0x69
  assert.deepEqual(b64decode("SGk="), new Uint8Array([0x48, 0x69]));
});

test("round-trip: ランダムバイナリ (256 entries)", () => {
  const orig = new Uint8Array(256);
  for (let i = 0; i < 256; i++) orig[i] = i;
  const decoded = b64decode(b64encode(orig));
  assert.deepEqual(decoded, orig);
});

test("round-trip: 16B IV 相当", () => {
  const iv = new Uint8Array([
    0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff, 0x11, 0x22,
    0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0x00,
  ]);
  assert.deepEqual(b64decode(b64encode(iv)), iv);
});

test("b64decode: 不正な base64 で throw", () => {
  // atob は不正文字列で例外を投げる
  assert.throws(() => b64decode("!!!not-base64!!!"));
});

test("api.js re-export 互換性 (既存 import パス維持)", async () => {
  const apiMod = await import(
    new URL("../../../app/static/js/crypto/api.js", import.meta.url).href
  );
  // api.js 経由でも同じ関数が取れる
  assert.equal(typeof apiMod.b64encode, "function");
  assert.equal(typeof apiMod.b64decode, "function");
  assert.equal(apiMod.b64encode(new Uint8Array([1])), b64encode(new Uint8Array([1])));
});
