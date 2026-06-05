// E7 (#114) 再ラップ: record.js の rewrapRecord / rewrapBlob を実 WebCrypto で検証。
//
// 実運用: サーバが temp-MK で暗号化したデータを、クライアントが temp-MK で復号 →
// 本物 MK で再暗号化する。AAD は不変、key と iv のみ変わる。Worker の rewrap op を
// 模す real client で、本物 MK で復号一致・temp-MK では不可・AAD 強制・冪等を確認。

import { test } from "node:test";
import assert from "node:assert/strict";

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD, rewrapRecord, rewrapBlob } = await import(REC.href);

const subtle = globalThis.crypto.subtle;
const TE = new TextEncoder();
const TD = new TextDecoder();

async function importKey(raw, usages) {
  return subtle.importKey("raw", raw, "AES-GCM", false, usages);
}
async function gcmEnc(key, pt, aad) {
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(
    await subtle.encrypt({ name: "AES-GCM", iv, additionalData: aad }, key, pt),
  );
  return { iv, ciphertext: ct };
}
async function gcmDec(key, ct, iv, aad) {
  return new Uint8Array(
    await subtle.decrypt({ name: "AES-GCM", iv, additionalData: aad }, key, ct),
  );
}

// Worker の rewrap (temp-MK 復号 → 本物 MK 再暗号) を模す real client。
async function makeClient(tempRaw, realRaw) {
  // テストではサーバの temp-MK 暗号化も再現するため temp に encrypt 権限も付与する
  // (実 Worker の rewrapKey は decrypt のみ)。
  const tempKey = await importKey(tempRaw, ["encrypt", "decrypt"]);
  const realKey = await importKey(realRaw, ["encrypt", "decrypt"]);
  return {
    realKey,
    tempKey,
    async rewrap(ciphertext, iv, aad) {
      const pt = await gcmDec(tempKey, ciphertext, iv, aad); // temp-MK で復号
      return gcmEnc(realKey, pt, aad); // 本物 MK で再暗号 (新 iv)
    },
  };
}

const TEMP = new Uint8Array(32).fill(0x11);
const REAL = new Uint8Array(32).fill(0x22);

test("rewrapRecord: temp-MK→本物 MK 再ラップ後、本物 MK で復号一致", async () => {
  const client = await makeClient(TEMP, REAL);
  const aad = buildAAD("je", 7);
  const recordJson = TE.encode(JSON.stringify({ v: 1, date: "2026-02-15" }));
  // サーバが temp-MK で暗号化した状態を再現
  const orig = await gcmEnc(client.tempKey, recordJson, aad);

  const re = await rewrapRecord(client, orig.ciphertext, orig.iv, aad);
  // 本物 MK + 同一 AAD で復号 → 元の平文
  const dec = await gcmDec(client.realKey, re.blob, re.iv, aad);
  assert.equal(TD.decode(dec), JSON.stringify({ v: 1, date: "2026-02-15" }));
  // iv は新規 (再暗号で変わる)
  assert.notDeepEqual(re.iv, orig.iv);
});

test("rewrapRecord: 再ラップ後は temp-MK では復号できない (真の鍵切替)", async () => {
  const client = await makeClient(TEMP, REAL);
  const aad = buildAAD("me", 3);
  const orig = await gcmEnc(client.tempKey, TE.encode("{}"), aad);
  const re = await rewrapRecord(client, orig.ciphertext, orig.iv, aad);
  await assert.rejects(() => gcmDec(client.tempKey, re.blob, re.iv, aad));
});

test("rewrapRecord: AAD 不一致は本物 MK 復号で失敗 (AAD 不変が強制される)", async () => {
  const client = await makeClient(TEMP, REAL);
  const aad = buildAAD("je", 7);
  const orig = await gcmEnc(client.tempKey, TE.encode("{}"), aad);
  const re = await rewrapRecord(client, orig.ciphertext, orig.iv, aad);
  await assert.rejects(() => gcmDec(client.realKey, re.blob, re.iv, buildAAD("je", 8)));
});

test("rewrapRecord: 既に本物 MK 済 (temp-MK で復号不可) は reject = skip 判定", async () => {
  const client = await makeClient(TEMP, REAL);
  const aad = buildAAD("je", 7);
  // 本物 MK で暗号化済の blob を temp-MK で rewrap しようとすると失敗
  const already = await gcmEnc(client.realKey, TE.encode("{}"), aad);
  await assert.rejects(() => rewrapRecord(client, already.ciphertext, already.iv, aad));
});

test("rewrapBlob: 画像 inline-iv (iv||ct||tag) を再ラップし本物 MK で復号一致", async () => {
  const client = await makeClient(TEMP, REAL);
  const aad = buildAAD("vimg", 2, 1234567890123n);
  const image = new Uint8Array([0xff, 0xd8, 0xff, ...Array(500).keys()]);
  const enc = await gcmEnc(client.tempKey, image, aad);
  const blobWithIv = new Uint8Array(12 + enc.ciphertext.length);
  blobWithIv.set(enc.iv, 0);
  blobWithIv.set(enc.ciphertext, 12);

  const re = await rewrapBlob(client, blobWithIv, aad);
  assert.equal(re.length, 12 + enc.ciphertext.length);
  // 先頭 12B を iv として本物 MK で復号 → 元画像
  const dec = await gcmDec(client.realKey, re.subarray(12), re.subarray(0, 12), aad);
  assert.deepEqual(dec, image);
});

test("rewrapBlob: 短すぎる blob は throw", async () => {
  const client = await makeClient(TEMP, REAL);
  await assert.rejects(
    () => rewrapBlob(client, new Uint8Array(10), buildAAD("vimg", 1, 1n)),
    /invalid image blob/,
  );
});

test("rewrapRecord/rewrapBlob: client.rewrap 不在で throw", async () => {
  await assert.rejects(
    () => rewrapRecord({}, new Uint8Array(16), new Uint8Array(12), new Uint8Array()),
    /client\.rewrap/,
  );
  await assert.rejects(
    () => rewrapBlob({}, new Uint8Array(40), new Uint8Array()),
    /client\.rewrap/,
  );
});
