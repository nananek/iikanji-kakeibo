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

test("argument validation: userId が string で throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => fetchJournalsForYear({ client, userId: "abc", fiscalYear: 2026 }),
    /userId must be a number or bigint/,
  );
});

test("argument validation: userId が unsafe Number で throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => fetchJournalsForYear({
      client, userId: Number.MAX_SAFE_INTEGER + 1, fiscalYear: 2026,
    }),
    /safe integer/,
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

test("AAD すり替え (別 user_id) は平文フォールバックする (全件 reject しない)", async () => {
  const client = makeMockClient();
  const userId = 1;
  const entry = await makeEncryptedEntry(client, userId, 100, {
    v: 1, date: "2026-05-22", description: "正常",
    source: "journal", fiscal_year: 2026,
  });
  // 平文フィールドは API レスポンスに null だが、復号失敗時は body=null →
  // null フォールバック値が透過的に返る (= 個別 entry は読めなくなるが
  // 全件取得自体は成功する)
  const fetchImpl = makeFetch([entry]);
  const result = await fetchJournalsForYear({
    client, userId: 2, fiscalYear: 2026, fetchImpl,  // userId mismatch
  });
  assert.equal(result.length, 1);
  // 平文フォールバック値が透過 (null だが throw はしない)
  assert.equal(result[0].id, 100);
  assert.equal(result[0].date, null);
  assert.equal(result[0].description, null);
});

test("encrypted line に id が無くても全件取得は成功し、line は平文フォールバック", async () => {
  // C-1 修正: _normalizeLine の throw が Promise.all を突き抜けて
  // fetchJournalsForYear 全体を reject させないよう、line 単位で catch して
  // 平文フォールバックに局所化している。
  const client = makeMockClient();
  const userId = 1;
  const entryId = 100;
  const entry = await makeEncryptedEntry(client, userId, entryId, {
    v: 1, date: "2026-05-22", description: "test entry", fiscal_year: 2026,
  });
  const lineWithoutId = await makeEncryptedLine(
    client, userId, entryId, 200,
    { account_code: "5010", debit_amount: 100, credit_amount: 0 },
  );
  delete lineWithoutId.id;
  // 平文 fallback 値もセット (本来 dual-read で平文があるはずだが、
  // ここではテストのため明示的にセット)
  lineWithoutId.account_code = "PLAINTEXT_5010";
  lineWithoutId.debit = 999;
  lineWithoutId.credit = 0;
  entry.lines = [lineWithoutId];
  const fetchImpl = makeFetch([entry]);
  const result = await fetchJournalsForYear({
    client, userId, fiscalYear: 2026, fetchImpl,
  });
  // 全件取得は成功 (1 件)
  assert.equal(result.length, 1);
  // entry 自体は復号成功
  assert.equal(result[0].description, "test entry");
  // 該当 line は平文フォールバック
  assert.equal(result[0].lines[0].account_code, "PLAINTEXT_5010");
  assert.equal(result[0].lines[0].debit, 999);
});

test("batch_id も dual-read で平文フォールバック (W-1 修正)", async () => {
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 1, fiscal_year: 2026,
    date: "2026-01-01", description: "未移行",
    source: "csv", batch_id: "test-batch-uuid-123",
    encrypted_blob: null, blob_iv: null,
    lines: [],
  }]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result[0].batch_id, "test-batch-uuid-123");
});

test("fiscal_period が API レスポンスから取れる (dual-read)", async () => {
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 1, fiscal_year: 2026,
    date: "2026-01-01", description: "期首",
    source: "journal", fiscal_period: 0,  // 期首振戻
    encrypted_blob: null, blob_iv: null,
    lines: [],
  }]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result[0].fiscal_period, 0);
});

test("ページネ: total=0 + journals 非空のサーババグでも全件取得する", async () => {
  // サーバが誤って total=0 を返したが journals は実は存在するケース。
  // 旧実装 (body.total || 0) だと初回で break して 1 ページのみだったが、
  // journals.length > 0 なら続行するように修正済。
  const client = makeMockClient();
  let page_calls = 0;
  const fetchImpl = async (url) => {
    page_calls++;
    const u = new URL(url, "http://x");
    const page = parseInt(u.searchParams.get("page") || "1", 10);
    if (page === 1) {
      return {
        ok: true,
        json: async () => ({
          ok: true,
          journals: [{
            id: page * 10, fiscal_year: 2026,
            date: `2026-01-0${page}`, description: `entry-${page}`,
            source: "journal", encrypted_blob: null, blob_iv: null,
            lines: [],
          }],
          total: 0,  // サーババグ
        }),
      };
    }
    // 2 ページ目以降は empty
    return {
      ok: true,
      json: async () => ({ ok: true, journals: [], total: 0 }),
    };
  };
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  // page 1 で 1 件取得、page 2 で空 → break。1 件取得できる。
  assert.equal(result.length, 1);
  assert.equal(result[0].id, 10);
});
