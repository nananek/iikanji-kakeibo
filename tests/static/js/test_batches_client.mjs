// batches_client.js (E3-F PR-D-6-3b-2) の単体テスト。
//
// fetch をモック + SharedCryptoClient をモックして、バッチ entry blob の復号 +
// 種別ラベル (source) / 日付範囲 (date_from / date_to) の導出 + closing 合成 +
// 復号失敗フォールバックを検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const CLIENT = new URL(
  "../../../app/static/js/crypto/batches_client.js",
  import.meta.url,
);
const { fetchBatches } = await import(CLIENT.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode } = await import(B64.href);


// --- mock SharedCryptoClient (test_journals_client.mjs と同形式) ---

function makeMockClient() {
  const aadStore = new Map();
  function _key(b) { return Array.from(b.slice(0, 32)).join(","); }
  return {
    async encrypt(plaintext, aad) {
      const iv = new Uint8Array(12);
      crypto.getRandomValues(iv);
      const ciphertext = new Uint8Array(plaintext.length + 16);
      ciphertext.set(plaintext, 0);
      crypto.getRandomValues(ciphertext.subarray(plaintext.length));
      aadStore.set(_key(ciphertext), new Uint8Array(aad || []));
      return { ciphertext, iv };
    },
    async decrypt(ciphertext, iv, aad) {
      const expected = aadStore.get(_key(ciphertext));
      const actual = new Uint8Array(aad || []);
      if (!expected || expected.length !== actual.length) {
        throw new Error("decrypt: AAD mismatch");
      }
      for (let i = 0; i < expected.length; i++) {
        if (expected[i] !== actual[i]) throw new Error("decrypt: AAD mismatch");
      }
      const plen = ciphertext.length - 16;
      return { plaintext: ciphertext.slice(0, plen) };
    },
  };
}


// entry の暗号化 blob を mock API レスポンス形式で組み立てる。
async function makeEncEntry(client, userId, id, fiscalYear, recordBody) {
  const aad = buildAAD("je", userId);
  const pt = new TextEncoder().encode(JSON.stringify(recordBody));
  const { ciphertext, iv } = await client.encrypt(pt, aad);
  return {
    id,
    fiscal_year: fiscalYear,
    is_closing: false,
    encrypted_blob: b64encode(ciphertext),
    blob_iv: b64encode(iv),
  };
}


function makeFetch(batches) {
  return async function (url, _init) {
    assert.equal(url, "/api/v1/journals/batches");
    return { ok: true, json: async () => ({ ok: true, batches }) };
  };
}


// ============ テスト ============

test("引数検証: client なしで throw", async () => {
  await assert.rejects(
    () => fetchBatches({ userId: 1 }),
    /client.*required/,
  );
});

test("引数検証: userId なしで throw", async () => {
  const client = makeMockClient();
  await assert.rejects(() => fetchBatches({ client }), /userId is required/);
});

test("引数検証: userId が string で throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => fetchBatches({ client, userId: "abc" }),
    /userId must be a number or bigint/,
  );
});

test("空レスポンスで空配列を返す", async () => {
  const client = makeMockClient();
  const result = await fetchBatches({
    client, userId: 1, fetchImpl: makeFetch([]),
  });
  assert.deepEqual(result, []);
});

test("entry blob を復号して source / 日付範囲を導出", async () => {
  const client = makeMockClient();
  const userId = 7;
  const e1 = await makeEncEntry(client, userId, 1, 2026, {
    v: 1, date: "2026-02-15", description: "csv 1", source: "csv",
  });
  const e2 = await makeEncEntry(client, userId, 2, 2026, {
    v: 1, date: "2026-02-03", description: "csv 2", source: "csv",
  });
  const e3 = await makeEncEntry(client, userId, 3, 2026, {
    v: 1, date: "2026-02-28", description: "csv 3", source: "csv",
  });
  const batches = [{
    batch_id: "bid-csv", count: 3, imported_at: "2026-02-15T10:30:00",
    is_closing: false, deletable: true, delete_reason: "",
    entries: [e1, e2, e3],
  }];
  const result = await fetchBatches({
    client, userId, fetchImpl: makeFetch(batches),
  });
  assert.equal(result.length, 1);
  const b = result[0];
  assert.equal(b.batch_id, "bid-csv");
  assert.equal(b.source, "csv");
  assert.equal(b.count, 3);
  assert.equal(b.imported_at, "2026-02-15T10:30:00");
  assert.equal(b.date_from, "2026-02-03"); // min
  assert.equal(b.date_to, "2026-02-28");   // max
  assert.equal(b.deletable, true);
  assert.equal(b.is_closing, false);
});

test("closing バッチは保持列から source=closing / date=年末 を合成", async () => {
  // closing 仕訳は暗号化不能 = encrypted_blob 空。is_closing / fiscal_year から
  // source="closing" / date=`${fiscal_year}-12-31` を合成する。
  const client = makeMockClient();
  const batches = [{
    batch_id: "bid-closing", count: 2, imported_at: "2026-12-31T23:59:00",
    is_closing: true, deletable: false,
    delete_reason: "損益振替（自動生成）は削除できません",
    entries: [
      { id: 10, fiscal_year: 2026, is_closing: true,
        encrypted_blob: null, blob_iv: null },
      { id: 11, fiscal_year: 2026, is_closing: true,
        encrypted_blob: null, blob_iv: null },
    ],
  }];
  const result = await fetchBatches({
    client, userId: 1, fetchImpl: makeFetch(batches),
  });
  const b = result[0];
  assert.equal(b.is_closing, true);
  assert.equal(b.source, "closing");
  assert.equal(b.date_from, "2026-12-31");
  assert.equal(b.date_to, "2026-12-31");
  assert.equal(b.deletable, false);
  assert.equal(b.delete_reason, "損益振替（自動生成）は削除できません");
});

test("復号失敗 (別 userId) は source 空 / 日付 null にフォールバック", async () => {
  const client = makeMockClient();
  const e1 = await makeEncEntry(client, 1, 1, 2026, {
    v: 1, date: "2026-03-01", description: "x", source: "web",
  });
  const batches = [{
    batch_id: "bid-web", count: 1, imported_at: "2026-03-01T09:00:00",
    is_closing: false, deletable: true, delete_reason: "",
    entries: [e1],
  }];
  // userId mismatch → AAD 不一致で復号失敗 → body=null → 非 closing は ""/null
  const result = await fetchBatches({
    client, userId: 2, fetchImpl: makeFetch(batches),
  });
  const b = result[0];
  assert.equal(b.source, "");
  assert.equal(b.date_from, null);
  assert.equal(b.date_to, null);
  assert.equal(b.count, 1); // 保持列メタは生きる
});

test("HTTP エラーで throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({
    ok: false, status: 500, json: async () => ({ error: "Internal" }),
  });
  await assert.rejects(
    () => fetchBatches({ client, userId: 1, fetchImpl }),
    /HTTP 500/,
  );
});
