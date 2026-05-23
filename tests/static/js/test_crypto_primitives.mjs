// E2EE 暗号プリミティブの Node 単体テスト (E1 #108)。
//
// 実行: node --test tests/static/js/test_crypto_primitives.mjs
//
// Node 22+ の globalThis.crypto (WebCrypto API) を使用。worker.js 本体は
// `self.onmessage` を持つため Node 直接実行できないが、primitives.js は
// 純粋関数で構成されているため Node でテスト可能。

import { test } from "node:test";
import assert from "node:assert/strict";

const PRIM_URL = new URL(
  "../../../app/static/js/crypto/primitives.js",
  import.meta.url,
);
const {
  aesGcmDecrypt,
  aesGcmEncrypt,
  importAesKey,
  isPlainObject,
  isUint8,
  unwrapMasterKey,
  wrapMasterKey,
} = await import(PRIM_URL.href);


function randomBytes(n) {
  return crypto.getRandomValues(new Uint8Array(n));
}

function utf8(s) {
  return new TextEncoder().encode(s);
}


test("isUint8 / isPlainObject 型判定", () => {
  assert.equal(isUint8(new Uint8Array(0)), true);
  assert.equal(isUint8([1, 2, 3]), false);
  assert.equal(isUint8("string"), false);
  assert.equal(isUint8(null), false);
  assert.equal(isPlainObject({ a: 1 }), true);
  assert.equal(isPlainObject([]), false);
  assert.equal(isPlainObject(null), false);
});


test("importAesKey は 32B 以外を弾く", async () => {
  await assert.rejects(
    () => importAesKey(randomBytes(16), ["encrypt"]),
    /must be Uint8Array of 32 bytes/,
  );
});


test("aesGcm encrypt → decrypt の往復で平文一致", async () => {
  const key = await importAesKey(randomBytes(32), ["encrypt", "decrypt"]);
  const plaintext = utf8("hello world");
  const { iv, ciphertext } = await aesGcmEncrypt(key, plaintext);
  assert.equal(iv.byteLength, 12);
  assert.equal(ciphertext.byteLength, plaintext.byteLength + 16); // AES-GCM tag
  const pt2 = await aesGcmDecrypt(key, ciphertext, iv);
  assert.deepEqual([...pt2], [...plaintext]);
});


test("aesGcm AAD 一致で復号成功・不一致で失敗", async () => {
  const key = await importAesKey(randomBytes(32), ["encrypt", "decrypt"]);
  const plaintext = utf8("data");
  const aad = utf8("context:1");
  const { iv, ciphertext } = await aesGcmEncrypt(key, plaintext, aad);
  // 一致
  const pt = await aesGcmDecrypt(key, ciphertext, iv, aad);
  assert.deepEqual([...pt], [...plaintext]);
  // 不一致 → 復号失敗
  await assert.rejects(
    () => aesGcmDecrypt(key, ciphertext, iv, utf8("context:2")),
  );
});


test("aesGcm 異なる IV では復号失敗", async () => {
  const key = await importAesKey(randomBytes(32), ["encrypt", "decrypt"]);
  const { ciphertext } = await aesGcmEncrypt(key, utf8("data"));
  const wrongIv = randomBytes(12);
  await assert.rejects(() => aesGcmDecrypt(key, ciphertext, wrongIv));
});


test("aesGcmDecrypt は IV 長 12B を強制", async () => {
  const key = await importAesKey(randomBytes(32), ["encrypt", "decrypt"]);
  await assert.rejects(
    () => aesGcmDecrypt(key, new Uint8Array(16), randomBytes(8)),
    /iv must be Uint8Array of 12 bytes/,
  );
});


test("wrap → unwrap で rawMk が復元される", async () => {
  const rawMk = randomBytes(32);
  const original = new Uint8Array(rawMk); // コピー保持
  const rawWk = randomBytes(32);
  const wkCopy = new Uint8Array(rawWk); // wrap 後 unwrap 用に保持
  const { iv, ciphertext } = await wrapMasterKey(rawMk, rawWk);
  const unwrapped = await unwrapMasterKey(ciphertext, iv, wkCopy);
  assert.deepEqual([...unwrapped], [...original]);
});


test("unwrap は wrap で使った wrappingKey と異なる鍵では失敗", async () => {
  const rawMk = randomBytes(32);
  const rawWk = randomBytes(32);
  const wkCopy = new Uint8Array(rawWk);
  const { iv, ciphertext } = await wrapMasterKey(rawMk, rawWk);
  const wrongWk = randomBytes(32);
  await assert.rejects(() => unwrapMasterKey(ciphertext, iv, wrongWk));
});


test("wrap 結果は毎回異なる (IV が乱数)", async () => {
  const rawMk = randomBytes(32);
  const rawWk = randomBytes(32);
  const a = await wrapMasterKey(new Uint8Array(rawMk), new Uint8Array(rawWk));
  const b = await wrapMasterKey(new Uint8Array(rawMk), new Uint8Array(rawWk));
  // 同じ MK + 同じ wrappingKey でも IV が違うので ciphertext も異なる
  assert.notDeepEqual([...a.iv], [...b.iv]);
  assert.notDeepEqual([...a.ciphertext], [...b.ciphertext]);
});


test("wrapMasterKey は rawMk 32B 以外を弾く", async () => {
  await assert.rejects(
    () => wrapMasterKey(randomBytes(16), randomBytes(32)),
    /rawMk must be Uint8Array of 32 bytes/,
  );
});


test("wrapMasterKey は rawWrappingKey 32B 以外を弾く", async () => {
  await assert.rejects(
    () => wrapMasterKey(randomBytes(32), randomBytes(16)),
    /rawWrappingKey must be Uint8Array of 32 bytes/,
  );
});


test("unwrapMasterKey は rawWrappingKey 32B 以外を弾く", async () => {
  // 有効な wrap 結果を用意
  const rawMk = randomBytes(32);
  const rawWk = randomBytes(32);
  const { iv, ciphertext } = await wrapMasterKey(rawMk, rawWk);
  // wrongSize な wrappingKey で unwrap → reject
  await assert.rejects(
    () => unwrapMasterKey(ciphertext, iv, randomBytes(24)),
    /rawWrappingKey must be Uint8Array of 32 bytes/,
  );
});
