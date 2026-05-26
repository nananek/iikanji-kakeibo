// balance_cache_blobs_client.js (Phase E3-E-2) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/balance_cache_blobs_client.js",
  import.meta.url,
);
const {
  fetchBalanceCacheBlobs,
  saveBalanceCacheBlob,
  deleteBalanceCacheBlobs,
} = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);

const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode } = await import(B64.href);


// --- mock SharedCryptoClient (AAD round-trip) ---

function makeMockClient() {
  const aadStore = new Map();
  function key(b) { return Array.from(b.slice(0, 32)).join(","); }
  return {
    async encrypt(plaintext, aad) {
      const iv = new Uint8Array(12);
      crypto.getRandomValues(iv);
      // 簡略実装: ciphertext = plaintext + 16 byte ランダム (mock)
      const ciphertext = new Uint8Array(plaintext.length + 16);
      ciphertext.set(plaintext, 0);
      crypto.getRandomValues(ciphertext.subarray(plaintext.length));
      aadStore.set(key(ciphertext), new Uint8Array(aad || []));
      return { ciphertext, iv };
    },
    async decrypt(ciphertext, iv, aad) {
      const expected = aadStore.get(key(ciphertext));
      const actual = new Uint8Array(aad || []);
      if (!expected || expected.length !== actual.length) {
        throw new Error("decrypt: AAD mismatch");
      }
      for (let i = 0; i < expected.length; i++) {
        if (expected[i] !== actual[i]) throw new Error("decrypt: AAD mismatch");
      }
      // plaintext は ciphertext の先頭 (length - 16) byte
      return { plaintext: ciphertext.slice(0, ciphertext.length - 16) };
    },
  };
}


function mockFetch(routes) {
  return async function(url, opts) {
    for (const [pattern, handler] of routes) {
      if (typeof pattern === "string") {
        if (url === pattern) return handler(url, opts);
      } else if (pattern.test(url)) {
        return handler(url, opts);
      }
    }
    return { ok: false, status: 404, json: async () => ({ error: "not found" }) };
  };
}


// --- fetchBalanceCacheBlobs ---


test("fetchBalanceCacheBlobs: GET → 復号 → period 別 dict", async () => {
  const client = makeMockClient();
  const userId = 7;
  const fiscalYear = 2026;
  // 暗号化 (period=3) + (period=12) の 2 blob を準備
  const enc3 = new TextEncoder().encode(JSON.stringify({ "1010": [100, 200] }));
  const aad3 = buildAAD("bcb", userId, fiscalYear * 100 + 3);
  const r3 = await client.encrypt(enc3, aad3);

  const enc12 = new TextEncoder().encode(JSON.stringify({ "5010": [50, 0] }));
  const aad12 = buildAAD("bcb", userId, fiscalYear * 100 + 12);
  const r12 = await client.encrypt(enc12, aad12);

  const fetchImpl = mockFetch([
    [/balance-cache-blobs\?year=2026/, async () => ({
      ok: true,
      json: async () => ({
        blobs: [
          { year: 2026, period: 3, encrypted_blob: b64encode(r3.ciphertext), blob_iv: b64encode(r3.iv) },
          { year: 2026, period: 12, encrypted_blob: b64encode(r12.ciphertext), blob_iv: b64encode(r12.iv) },
        ],
      }),
    })],
  ]);

  const out = await fetchBalanceCacheBlobs({
    client, userId, fiscalYear, fetchImpl,
  });
  assert.deepEqual(out, {
    3: { "1010": [100, 200] },
    12: { "5010": [50, 0] },
  });
});


test("fetchBalanceCacheBlobs: 復号失敗 (AAD 違い) はその blob を skip", async () => {
  const client = makeMockClient();
  const userId = 7;
  const fiscalYear = 2026;
  // 別の userId で暗号化したものを返す = AAD mismatch
  const enc = new TextEncoder().encode(JSON.stringify({ "1010": [100, 0] }));
  const aadWrong = buildAAD("bcb", 999, fiscalYear * 100 + 3);
  const r = await client.encrypt(enc, aadWrong);

  const fetchImpl = mockFetch([
    [/balance-cache-blobs/, async () => ({
      ok: true,
      json: async () => ({
        blobs: [
          { year: 2026, period: 3, encrypted_blob: b64encode(r.ciphertext), blob_iv: b64encode(r.iv) },
        ],
      }),
    })],
  ]);

  const out = await fetchBalanceCacheBlobs({
    client, userId, fiscalYear, fetchImpl,
  });
  // skip された結果、空 dict
  assert.deepEqual(out, {});
});


test("fetchBalanceCacheBlobs: 別 year の blob は混入させない", async () => {
  const client = makeMockClient();
  const userId = 7;
  // サーバが誤って 2025 の blob を返してきた場合、混入させない
  const enc = new TextEncoder().encode(JSON.stringify({ "1010": [1, 0] }));
  const aad = buildAAD("bcb", userId, 2025 * 100 + 3);
  const r = await client.encrypt(enc, aad);

  const fetchImpl = mockFetch([
    [/balance-cache-blobs/, async () => ({
      ok: true,
      json: async () => ({
        blobs: [
          { year: 2025, period: 3, encrypted_blob: b64encode(r.ciphertext), blob_iv: b64encode(r.iv) },
        ],
      }),
    })],
  ]);
  const out = await fetchBalanceCacheBlobs({
    client, userId, fiscalYear: 2026, fetchImpl,
  });
  assert.deepEqual(out, {});
});


test("fetchBalanceCacheBlobs: HTTP error は throw", async () => {
  const client = makeMockClient();
  const fetchImpl = mockFetch([
    [/.*/, async () => ({
      ok: false, status: 500,
      json: async () => ({ error: "internal" }),
    })],
  ]);
  await assert.rejects(
    fetchBalanceCacheBlobs({ client, userId: 7, fiscalYear: 2026, fetchImpl }),
    /HTTP 500/,
  );
});


test("fetchBalanceCacheBlobs: 無効な userId / fiscalYear で throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    fetchBalanceCacheBlobs({ client, userId: "bad", fiscalYear: 2026, fetchImpl: globalThis.fetch }),
    /userId/,
  );
  await assert.rejects(
    fetchBalanceCacheBlobs({ client, userId: 7, fiscalYear: 1800, fetchImpl: globalThis.fetch }),
    /year/,
  );
});


// --- saveBalanceCacheBlob ---


test("saveBalanceCacheBlob: 暗号化 → PUT", async () => {
  const client = makeMockClient();
  let captured;
  const fetchImpl = mockFetch([
    [/balance-cache-blobs\/2026\/3$/, async (url, opts) => {
      captured = { url, body: JSON.parse(opts.body), method: opts.method };
      return {
        ok: true,
        json: async () => ({ ok: true, updated_at: "2026-05-26T03:00:00+00:00" }),
      };
    }],
  ]);
  const res = await saveBalanceCacheBlob({
    client, userId: 7, year: 2026, period: 3,
    balances: { "1010": [100, 200] },
    fetchImpl,
  });
  assert.equal(res.ok, true);
  assert.equal(captured.method, "PUT");
  assert.ok(captured.body.encrypted_blob);
  assert.ok(captured.body.blob_iv);
});


test("saveBalanceCacheBlob: period=17 で throw (0-16 のみ)", async () => {
  const client = makeMockClient();
  await assert.rejects(
    saveBalanceCacheBlob({
      client, userId: 7, year: 2026, period: 17,
      balances: {}, fetchImpl: globalThis.fetch,
    }),
    /period/,
  );
});


test("saveBalanceCacheBlob: balances 未指定で throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    saveBalanceCacheBlob({
      client, userId: 7, year: 2026, period: 3,
      balances: null, fetchImpl: globalThis.fetch,
    }),
    /balances/,
  );
});


// --- deleteBalanceCacheBlobs ---


test("deleteBalanceCacheBlobs: 年指定で DELETE", async () => {
  let captured;
  const fetchImpl = mockFetch([
    [/balance-cache-blobs\/2026$/, async (url, opts) => {
      captured = { url, method: opts.method };
      return { ok: true, json: async () => ({ ok: true, deleted: 5 }) };
    }],
  ]);
  const res = await deleteBalanceCacheBlobs({ year: 2026, fetchImpl });
  assert.equal(res.deleted, 5);
  assert.equal(captured.method, "DELETE");
  assert.match(captured.url, /\/2026$/);
});


test("deleteBalanceCacheBlobs: fromPeriod 指定で query parameter", async () => {
  let captured;
  const fetchImpl = mockFetch([
    [/balance-cache-blobs/, async (url) => {
      captured = url;
      return { ok: true, json: async () => ({ ok: true, deleted: 2 }) };
    }],
  ]);
  await deleteBalanceCacheBlobs({ year: 2026, fromPeriod: 6, fetchImpl });
  assert.match(captured, /from_period=6/);
});


test("deleteBalanceCacheBlobs: 無効 year で throw", async () => {
  await assert.rejects(
    deleteBalanceCacheBlobs({ year: 1800, fetchImpl: globalThis.fetch }),
    /year/,
  );
});


test("deleteBalanceCacheBlobs: 無効 fromPeriod で throw", async () => {
  await assert.rejects(
    deleteBalanceCacheBlobs({ year: 2026, fromPeriod: 17, fetchImpl: globalThis.fetch }),
    /period/,
  );
});
