// WebAuthn PRF ラッパーの Node 単体テスト。
//
// navigator.credentials.get は Node にないため、Credential.getClientExtensionResults
// が返す形のオブジェクトを直接渡して動作検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const URL_ = new URL(
  "../../../app/static/js/crypto/webauthn_prf.js",
  import.meta.url,
);
const {
  getPrfEvalInputBytes,
  buildPrfExtensionInput,
  extractPrfOutput,
  deriveKeyFromPrfOutput,
  deriveKeyFromCredential,
} = await import(URL_.href);


function randomBytes(n) {
  return crypto.getRandomValues(new Uint8Array(n));
}

/** WebAuthn Credential 風オブジェクト */
function makeCredential(first) {
  return {
    getClientExtensionResults() {
      return first === undefined ? {} : { prf: { results: { first } } };
    },
  };
}


test("getPrfEvalInputBytes は UTF-8 'iikanji-master-key-v1'", () => {
  const b = getPrfEvalInputBytes();
  assert.deepEqual(
    [...b],
    [...new TextEncoder().encode("iikanji-master-key-v1")],
  );
});


test("buildPrfExtensionInput は WebAuthn extensions 形式を返す", () => {
  const ext = buildPrfExtensionInput();
  assert.ok(ext.prf?.eval?.first instanceof Uint8Array);
  assert.deepEqual(
    [...ext.prf.eval.first],
    [...new TextEncoder().encode("iikanji-master-key-v1")],
  );
});


test("extractPrfOutput: Uint8Array が返るときはコピーが返る", () => {
  const src = randomBytes(32);
  const cred = makeCredential(src);
  const out = extractPrfOutput(cred);
  assert.ok(out instanceof Uint8Array);
  assert.deepEqual([...out], [...src]);
  // コピーである (元を変更しても out は変わらない)
  src.fill(0);
  assert.notDeepEqual([...out], [...src]);
});


test("extractPrfOutput: ArrayBuffer も Uint8Array に正規化", () => {
  const buf = randomBytes(32).buffer;
  const cred = makeCredential(buf);
  const out = extractPrfOutput(cred);
  assert.ok(out instanceof Uint8Array);
  assert.equal(out.byteLength, 32);
});


test("extractPrfOutput: Array (polyfill) も Uint8Array に正規化", () => {
  const arr = [1, 2, 3, 4];
  const cred = makeCredential(arr);
  const out = extractPrfOutput(cred);
  assert.ok(out instanceof Uint8Array);
  assert.deepEqual([...out], arr);
});


test("extractPrfOutput: PRF 非対応 (results なし) は null", () => {
  const cred = makeCredential(undefined);
  assert.equal(extractPrfOutput(cred), null);
});


test("extractPrfOutput: clientExtensionResults プロパティ (テスト用素オブジェクト) も読める", () => {
  const cred = { clientExtensionResults: { prf: { results: { first: new Uint8Array([7, 7]) } } } };
  const out = extractPrfOutput(cred);
  assert.deepEqual([...out], [7, 7]);
});


test("extractPrfOutput: 不明な型 (string 等) は throw", () => {
  const cred = makeCredential("not bytes");
  assert.throws(() => extractPrfOutput(cred), /unsupported prf\.results\.first type/);
});


test("deriveKeyFromPrfOutput は 32B を返し、決定的", async () => {
  const prf = randomBytes(32);
  const k1 = await deriveKeyFromPrfOutput(new Uint8Array(prf));
  const k2 = await deriveKeyFromPrfOutput(new Uint8Array(prf));
  assert.equal(k1.byteLength, 32);
  assert.deepEqual([...k1], [...k2]);
});


test("deriveKeyFromPrfOutput は異なる PRF 入力で異なる鍵", async () => {
  const k1 = await deriveKeyFromPrfOutput(randomBytes(32));
  const k2 = await deriveKeyFromPrfOutput(randomBytes(32));
  assert.notDeepEqual([...k1], [...k2]);
});


test("deriveKeyFromPrfOutput は info 違いで異なる鍵 (ドメイン分離)", async () => {
  const prf = randomBytes(32);
  const k1 = await deriveKeyFromPrfOutput(new Uint8Array(prf));
  const k2 = await deriveKeyFromPrfOutput(new Uint8Array(prf), { info: "iikanji-audit-v1" });
  assert.notDeepEqual([...k1], [...k2]);
});


test("deriveKeyFromPrfOutput: 空入力は reject", async () => {
  await assert.rejects(
    () => deriveKeyFromPrfOutput(new Uint8Array(0)),
    /non-empty Uint8Array/,
  );
});


test("deriveKeyFromCredential: PRF あり → 32B derived_key", async () => {
  const cred = makeCredential(randomBytes(32));
  const k = await deriveKeyFromCredential(cred);
  assert.ok(k instanceof Uint8Array);
  assert.equal(k.byteLength, 32);
});


test("deriveKeyFromCredential: PRF 非対応 → null (フォールバック誘導)", async () => {
  const cred = makeCredential(undefined);
  const k = await deriveKeyFromCredential(cred);
  assert.equal(k, null);
});


test("deriveKeyFromCredential: PRF 出力が同一なら同じ derived_key", async () => {
  const prf = new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                              17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]);
  const k1 = await deriveKeyFromCredential(makeCredential(new Uint8Array(prf)));
  const k2 = await deriveKeyFromCredential(makeCredential(new Uint8Array(prf)));
  assert.deepEqual([...k1], [...k2]);
});
