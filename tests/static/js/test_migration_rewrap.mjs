// E7 (#114) 再ラップフロー (rewrap_flow.js) の Node 単体/結合テスト。
//
// 実 WebCrypto で temp-MK 暗号データを用意し、偽 fetch でサーバを模して
// runRewrapMigration を end-to-end 実行する。再ラップ後の blob が本物 MK +
// 同一 AAD で復号でき、temp-MK では復号できない (真の鍵切替) ことを検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const FLOW = new URL(
  "../../../app/static/js/migration/rewrap_flow.js",
  import.meta.url,
);
const { runRewrapMigration, rewrapRecordItems } = await import(FLOW.href);

const RECORD = new URL(
  "../../../app/static/js/crypto/record.js",
  import.meta.url,
);
const { buildAAD } = await import(RECORD.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode, b64decode } = await import(B64.href);


// --- WebCrypto helpers ---

function rnd(n) { return crypto.getRandomValues(new Uint8Array(n)); }

async function importAes(raw, usages) {
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, usages);
}

async function enc(key, pt, iv, aad) {
  const ct = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: aad }, key, pt,
  );
  return new Uint8Array(ct);
}

async function dec(key, ct, iv, aad) {
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv, additionalData: aad }, key, ct,
  );
  return new Uint8Array(pt);
}

const TE = new TextEncoder();
const TD = new TextDecoder();


// 本物 MK 解錠済の client を模す (setRewrapKey/rewrap/clearRewrapKey)。
async function makeClient(masterRaw) {
  const masterKey = await importAes(masterRaw, ["encrypt", "decrypt"]);
  let rewrapKey = null;
  return {
    masterKey,
    async setRewrapKey(raw) { rewrapKey = await importAes(raw, ["decrypt"]); },
    async clearRewrapKey() { rewrapKey = null; },
    async rewrap(ct, iv, aad) {
      const pt = await dec(rewrapKey, ct, iv, aad);
      const niv = rnd(12);
      const nct = await enc(masterKey, pt, niv, aad);
      return { iv: niv, ciphertext: nct };
    },
    _hasRewrapKey() { return rewrapKey !== null; },
  };
}


// temp-MK で record (JSON) を暗号化して {encrypted_blob, blob_iv} (base64) を返す。
async function sealRecord(tempKey, obj, aad) {
  const iv = rnd(12);
  const ct = await enc(tempKey, TE.encode(JSON.stringify(obj)), iv, aad);
  return { encrypted_blob: b64encode(ct), blob_iv: b64encode(iv) };
}

// temp-MK で画像 (iv‖ct‖tag inline) を暗号化して Uint8Array を返す。
async function sealImage(tempKey, bytes, aad) {
  const iv = rnd(12);
  const ct = await enc(tempKey, bytes, iv, aad);
  const out = new Uint8Array(12 + ct.length);
  out.set(iv, 0);
  out.set(ct, 12);
  return out;
}


// --- rewrapRecordItems 単体 ---

test("rewrapRecordItems: temp-MK 項目を本物 MK へ再ラップ・本物 MK で復号一致", async () => {
  const masterRaw = rnd(32);
  const tempRaw = rnd(32);
  const client = await makeClient(masterRaw);
  await client.setRewrapKey(new Uint8Array(tempRaw));

  const tempKey = await importAes(tempRaw, ["encrypt", "decrypt"]);
  const aad = buildAAD("je", 7);
  const rec = { v: 1, date: "2026-01-01", description: "secret" };
  const sealed = await sealRecord(tempKey, rec, aad);

  const { items, skipped } = await rewrapRecordItems(client, [
    { key: { id: 1 }, blobB64: sealed.encrypted_blob, ivB64: sealed.blob_iv, aad },
  ]);
  assert.equal(skipped, 0);
  assert.equal(items.length, 1);
  assert.equal(items[0].id, 1);

  // 本物 MK + 同一 AAD で復号すると元 record に一致
  const pt = await dec(
    client.masterKey, b64decode(items[0].encrypted_blob),
    b64decode(items[0].blob_iv), aad,
  );
  assert.deepEqual(JSON.parse(TD.decode(pt)), rec);
});

test("rewrapRecordItems: 既に本物 MK 済 (temp-MK 復号不可) は skip", async () => {
  const masterRaw = rnd(32);
  const tempRaw = rnd(32);
  const client = await makeClient(masterRaw);
  await client.setRewrapKey(new Uint8Array(tempRaw));

  // masterKey で暗号化したデータ (= 既に再ラップ済) は temp-MK で復号できない
  const aad = buildAAD("je", 7);
  const iv = rnd(12);
  const ct = await enc(client.masterKey, TE.encode("{}"), iv, aad);

  const { items, skipped } = await rewrapRecordItems(client, [
    { key: { id: 1 }, blobB64: b64encode(ct), ivB64: b64encode(iv), aad },
  ]);
  assert.equal(items.length, 0);
  assert.equal(skipped, 1);
});


// --- runRewrapMigration end-to-end (偽 fetch) ---

function jsonResp(obj) {
  return { ok: true, status: 200, json: async () => obj };
}
function bufResp(bytes) {
  return { ok: true, status: 200, arrayBuffer: async () => bytes.buffer.slice(
    bytes.byteOffset, bytes.byteOffset + bytes.byteLength) };
}

test("runRewrapMigration: 全テーブル再ラップ + finalize + 本物 MK 復号一致", async () => {
  const userId = 42;
  const masterRaw = rnd(32);
  const tempRaw = rnd(32);
  const client = await makeClient(masterRaw);
  const tempKey = await importAes(tempRaw, ["encrypt", "decrypt"]);

  // seed: 1 年度の je(1)+jel(1)、me(1)、bcb(period3)、voucher(1: meta+log+img+thumb)
  const jeAad = buildAAD("je", userId);
  const jelAad = buildAAD("jel", userId);
  const meAad = buildAAD("me", userId);
  const bcbAad = buildAAD("bcb", userId, 2026 * 100 + 3);
  const aadId = 555n;
  const vmetaAad = buildAAD("vmeta", userId, aadId);
  const valogAad = buildAAD("valog", userId, aadId);
  const vimgAad = buildAAD("vimg", userId, aadId);
  const vthumbAad = buildAAD("vthumb", userId, aadId);

  const jeSealed = await sealRecord(tempKey, { v: 1, date: "2026-03-01" }, jeAad);
  const jelSealed = await sealRecord(tempKey, { v: 1, account_code: "5010" }, jelAad);
  const meSealed = await sealRecord(tempKey, { v: 1, patient_name: "X" }, meAad);
  const bcbSealed = await sealRecord(tempKey, { "1010": [100, 0] }, bcbAad);
  const vmetaSealed = await sealRecord(tempKey, { v: 1, original_filename: "r.jpg" }, vmetaAad);
  const valogSealed = await sealRecord(tempKey, { v: 1, note: "n" }, valogAad);
  const imgPlain = rnd(64);
  const thumbPlain = rnd(40);
  const imgSealed = await sealImage(tempKey, imgPlain, vimgAad);
  const thumbSealed = await sealImage(tempKey, thumbPlain, vthumbAad);

  const posted = { je: [], jel: [], me: [], bcb: [], vmeta: [], valog: [] };
  const images = [];
  let finalizeCalled = false;

  const fakeFetch = async (url, opts = {}) => {
    if (url === "/api/v1/migration/temp-mk") {
      return jsonResp({ active: true, temp_mk: b64encode(tempRaw) });
    }
    if (url.startsWith("/api/v1/journals?")) {
      return jsonResp({
        journals: [{
          id: 10, encrypted_blob: jeSealed.encrypted_blob, blob_iv: jeSealed.blob_iv,
          lines: [{ id: 20, encrypted_blob: jelSealed.encrypted_blob, blob_iv: jelSealed.blob_iv }],
        }],
        total: 1,
      });
    }
    if (url.startsWith("/api/v1/balance-cache-blobs?")) {
      return jsonResp({ blobs: [{
        year: 2026, period: 3,
        encrypted_blob: bcbSealed.encrypted_blob, blob_iv: bcbSealed.blob_iv,
      }] });
    }
    if (url === "/api/v1/medical-expenses") {
      return jsonResp({ expenses: [{
        id: 30, encrypted_blob: meSealed.encrypted_blob, blob_iv: meSealed.blob_iv,
      }] });
    }
    if (url.startsWith("/api/v1/migration/voucher-blobs")) {
      // page=1 で 1 件、page=2 で 0 件
      if (url.includes("page=1")) {
        return jsonResp({
          total: 1, page: 1, per_page: 200,
          vouchers: [{
            id: 99, aad_id: "555",
            encrypted_meta_blob: vmetaSealed.encrypted_blob, meta_iv: vmetaSealed.blob_iv,
            has_image: true, has_thumbnail: true,
            logs: [{ id: 77, encrypted_detail_blob: valogSealed.encrypted_blob, detail_iv: valogSealed.blob_iv }],
          }],
        });
      }
      return jsonResp({ total: 1, page: 2, per_page: 200, vouchers: [] });
    }
    if (url === "/api/v1/migration/voucher-image/99") return bufResp(imgSealed);
    if (url === "/api/v1/migration/voucher-image/99?size=thumb") return bufResp(thumbSealed);
    if (url === "/api/v1/migration/rewrap") {
      const body = JSON.parse(opts.body);
      posted[body.table].push(...body.items);
      return jsonResp({ ok: true, updated: body.items.length, skipped: 0 });
    }
    if (url === "/api/v1/migration/rewrap-image") {
      images.push(JSON.parse(opts.body));
      return jsonResp({ ok: true });
    }
    if (url === "/api/v1/migration/finalize") {
      finalizeCalled = true;
      return jsonResp({ ok: true, finalized: true });
    }
    throw new Error("unexpected fetch: " + url);
  };

  const progress = [];
  const summary = await runRewrapMigration({
    client, userId, years: [2026], fetchImpl: fakeFetch,
    onProgress: (d, t) => progress.push([d, t]),
  });

  assert.equal(summary.active, true);
  assert.equal(summary.je, 1);
  assert.equal(summary.jel, 1);
  assert.equal(summary.me, 1);
  assert.equal(summary.bcb, 1);
  assert.equal(summary.vmeta, 1);
  assert.equal(summary.valog, 1);
  assert.equal(summary.vimg, 1);
  assert.equal(summary.finalized, true);
  assert.ok(finalizeCalled);
  // 副鍵は finally で破棄される
  assert.equal(client._hasRewrapKey(), false);

  // 進捗は (years*2 + 3) = 5 ステップ
  assert.equal(progress[progress.length - 1][1], 5);

  // 本物 MK + 同一 AAD で je が復号できる
  const jePt = await dec(
    client.masterKey, b64decode(posted.je[0].encrypted_blob),
    b64decode(posted.je[0].blob_iv), jeAad,
  );
  assert.deepEqual(JSON.parse(TD.decode(jePt)), { v: 1, date: "2026-03-01" });

  // bcb は year/period キーで送られる
  assert.equal(posted.bcb[0].year, 2026);
  assert.equal(posted.bcb[0].period, 3);

  // 画像は本体+サムネが送られ、本物 MK で復号すると元画像に一致 (inline iv)
  assert.equal(images.length, 1);
  assert.equal(images[0].voucher_id, 99);
  const imgCt = b64decode(images[0].image_ct);
  const imgOut = await dec(client.masterKey, imgCt.subarray(12), imgCt.subarray(0, 12), vimgAad);
  assert.deepEqual([...imgOut], [...imgPlain]);
  const thCt = b64decode(images[0].thumb_ct);
  const thOut = await dec(client.masterKey, thCt.subarray(12), thCt.subarray(0, 12), vthumbAad);
  assert.deepEqual([...thOut], [...thumbPlain]);
});

test("runRewrapMigration: temp_mk 非 active なら何もせず {active:false}", async () => {
  const client = await makeClient(rnd(32));
  let setCalled = false;
  client.setRewrapKey = async () => { setCalled = true; };
  const fakeFetch = async (url) => {
    if (url === "/api/v1/migration/temp-mk") {
      return jsonResp({ active: false, temp_mk: null });
    }
    throw new Error("unexpected fetch: " + url);
  };
  const summary = await runRewrapMigration({
    client, userId: 1, years: [2026], fetchImpl: fakeFetch,
  });
  assert.deepEqual(summary, { active: false });
  assert.equal(setCalled, false);
});
