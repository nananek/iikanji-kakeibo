// journals_client.js (Phase E3-C-1b) の単体テスト。
//
// fetch をモック + SharedCryptoClient をモックして、dual-read + ページネ
// の挙動を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const CLIENT = new URL(
  "../../../app/static/js/crypto/journals_client.js",
  import.meta.url,
);
const { fetchJournalsForYear } = await import(CLIENT.href);

const REC = new URL(
  "../../../app/static/js/crypto/record.js",
  import.meta.url,
);
const { buildAAD } = await import(REC.href);

const B64 = new URL(
  "../../../app/static/js/crypto/b64.js",
  import.meta.url,
);
const { b64encode } = await import(B64.href);


// --- mock SharedCryptoClient ---
// encrypt 時に aad を Map に保管、decrypt 時に aad 一致確認 → fail なら throw。

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
        if (expected[i] !== actual[i]) {
          throw new Error("decrypt: AAD mismatch");
        }
      }
      const plen = ciphertext.length - 16;
      return { plaintext: ciphertext.slice(0, plen) };
    },
  };
}


// --- helper: encrypted entry + lines を mock API response 形式で組み立て ---

async function makeEncryptedEntry(client, userId, entryId, recordBody) {
  const aad = buildAAD("je", userId, entryId);
  const pt = new TextEncoder().encode(JSON.stringify(recordBody));
  const { ciphertext, iv } = await client.encrypt(pt, aad);
  return {
    id: entryId,
    fiscal_year: recordBody.fiscal_year ?? 2026,
    date: null,         // 暗号化済 → 平文 null
    description: null,
    source: null,
    encrypted_blob: b64encode(ciphertext),
    blob_iv: b64encode(iv),
    lines: [],
  };
}

async function makeEncryptedLine(client, userId, entryId, lineId, lineBody) {
  const aad = buildAAD("jel", userId, entryId, lineId);
  const pt = new TextEncoder().encode(JSON.stringify(lineBody));
  const { ciphertext, iv } = await client.encrypt(pt, aad);
  return {
    id: lineId,
    account_code: null,
    debit: 0,
    credit: 0,
    description: "",
    encrypted_blob: b64encode(ciphertext),
    blob_iv: b64encode(iv),
  };
}


// --- mock fetch (ページネ対応) ---

function makeFetch(allEntries, perPage = 100) {
  return async function (url, _init) {
    const u = new URL(url, "http://x");
    const page = parseInt(u.searchParams.get("page") || "1", 10);
    const pp = parseInt(u.searchParams.get("per_page") || "100", 10);
    const start = (page - 1) * pp;
    const slice = allEntries.slice(start, start + pp);
    return {
      ok: true,
      json: async () => ({
        ok: true,
        journals: slice,
        total: allEntries.length,
        page, per_page: pp,
      }),
    };
  };
}


// ============ テスト ============

test("argument validation: client なしで throw", async () => {
  await assert.rejects(
    () => fetchJournalsForYear({ userId: 1, fiscalYear: 2026 }),
    /client.*required/,
  );
});

test("argument validation: userId なしで throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => fetchJournalsForYear({ client, fiscalYear: 2026 }),
    /userId is required/,
  );
});

test("argument validation: fiscalYear 範囲外で throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => fetchJournalsForYear({ client, userId: 1, fiscalYear: 999 }),
    /1900\.\.2200/,
  );
});

test("空レスポンスで空配列を返す", async () => {
  const client = makeMockClient();
  const fetchImpl = makeFetch([]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.deepEqual(result, []);
});

test("encrypted entry + lines を復号して正規化する", async () => {
  const client = makeMockClient();
  const userId = 1;
  const entryId = 100;

  const entry = await makeEncryptedEntry(client, userId, entryId, {
    v: 1,
    date: "2026-05-22",
    description: "スーパー",
    source: "cashbook",
    batch_id: null,
    fiscal_period: 5,
    fiscal_year: 2026,
  });
  entry.lines = [
    await makeEncryptedLine(client, userId, entryId, 200, {
      account_code: "5010", debit_amount: 1000, credit_amount: 0,
      description: "食費",
    }),
    await makeEncryptedLine(client, userId, entryId, 201, {
      account_code: "1010", debit_amount: 0, credit_amount: 1000,
      description: "現金",
    }),
  ];

  const fetchImpl = makeFetch([entry]);
  const result = await fetchJournalsForYear({
    client, userId, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result.length, 1);
  const got = result[0];
  assert.equal(got.id, entryId);
  assert.equal(got.date, "2026-05-22");
  assert.equal(got.description, "スーパー");
  assert.equal(got.source, "cashbook");
  assert.equal(got.fiscal_period, 5);
  assert.equal(got.lines.length, 2);
  assert.equal(got.lines[0].account_code, "5010");
  assert.equal(got.lines[0].debit, 1000);
  assert.equal(got.lines[0].credit, 0);
  assert.equal(got.lines[1].account_code, "1010");
  assert.equal(got.lines[1].credit, 1000);
});

test("dual-read: blob/iv が null の entry は平文フォールバック", async () => {
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 1, fiscal_year: 2026,
    date: "2026-01-01", description: "未移行行",
    source: "journal", encrypted_blob: null, blob_iv: null,
    lines: [
      { account_code: "5010", debit: 500, credit: 0, description: "",
        encrypted_blob: null, blob_iv: null },
    ],
  }]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result.length, 1);
  assert.equal(result[0].date, "2026-01-01");
  assert.equal(result[0].description, "未移行行");
  assert.equal(result[0].lines[0].account_code, "5010");
  assert.equal(result[0].lines[0].debit, 500);
});

test("ページネーションで全件取得", async () => {
  // 150 件を 100/ページで 2 ページに分割
  const entries = [];
  for (let i = 1; i <= 150; i++) {
    entries.push({
      id: i, fiscal_year: 2026,
      date: `2026-01-${String((i % 28) + 1).padStart(2, "0")}`,
      description: `entry-${i}`, source: "journal",
      encrypted_blob: null, blob_iv: null,
      lines: [],
    });
  }
  const client = makeMockClient();
  const fetchImpl = makeFetch(entries, 100);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result.length, 150);
  assert.equal(result[0].description, "entry-1");
  assert.equal(result[149].description, "entry-150");
});

test("HTTP エラーで throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({
    ok: false, status: 500,
    json: async () => ({ error: "Internal" }),
  });
  await assert.rejects(
    () => fetchJournalsForYear({ client, userId: 1, fiscalYear: 2026, fetchImpl }),
    /HTTP 500/,
  );
});

test("AAD すり替え (別 user_id) で復号失敗 → throw", async () => {
  const client = makeMockClient();
  const userId = 1;
  const entry = await makeEncryptedEntry(client, userId, 100, {
    v: 1, date: "2026-05-22", description: "x",
    source: "journal", fiscal_year: 2026,
  });
  const fetchImpl = makeFetch([entry]);
  // fetchJournalsForYear に別 userId (2) を渡すと AAD が違って復号失敗
  await assert.rejects(
    () => fetchJournalsForYear({
      client, userId: 2, fiscalYear: 2026, fetchImpl,
    }),
    /AAD mismatch/,
  );
});
