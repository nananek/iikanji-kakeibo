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
const {
  fetchJournalsForYear, decryptEntryMeta, fetchEntryFields,
  decryptLineDescriptions, fetchEntryForDiff,
} = await import(CLIENT.href);

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
  // E3-F PR-A: AAD は Option B (user_id のみ、entry_id を含めない)。
  const aad = buildAAD("je", userId);
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
  const aad = buildAAD("jel", userId);
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

test("blob/iv が null の非 closing entry は date=null/description=空 (平文フォールバック廃止)", async () => {
  // E3-F PR-D-6-3b: API は平文 date/description/source/fiscal_period を返さない。
  // 復号不能 (blob/iv null) かつ非 closing の entry は date=null/description=""。
  // line.account_code 等の平文フォールバックは本 PR の対象外で継続する。
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 1, fiscal_year: 2026,
    is_closing: false, fiscal_month: 1,
    encrypted_blob: null, blob_iv: null,
    lines: [
      { account_code: "5010", debit: 500, credit: 0, description: "",
        encrypted_blob: null, blob_iv: null },
    ],
  }]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result.length, 1);
  assert.equal(result[0].date, null);
  assert.equal(result[0].description, "");
  assert.equal(result[0].source, "");
  assert.equal(result[0].fiscal_period, null);
  assert.equal(result[0].fiscal_month, 1);
  assert.equal(result[0].lines[0].account_code, "5010");
  assert.equal(result[0].lines[0].debit, 500);
});

test("closing 仕訳 (空 blob) は保持列から date/description/source/fiscal_period を合成", async () => {
  // E3-F PR-D-6-3b: サーバは MK を持たず closing を暗号化できないため encrypted_blob
  // は空。is_closing/fiscal_month/fiscal_year からクライアントが合成する。
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 9, fiscal_year: 2026,
    is_closing: true, fiscal_month: 16,
    encrypted_blob: "", blob_iv: "",
    lines: [
      { account_code: "3020", debit: 0, credit: 1000, description: "",
        encrypted_blob: null, blob_iv: null },
    ],
  }]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result.length, 1);
  assert.equal(result[0].is_closing, true);
  assert.equal(result[0].date, "2026-12-31");
  assert.equal(result[0].description, "損益振替仕訳（自動生成）");
  assert.equal(result[0].source, "closing");
  assert.equal(result[0].fiscal_period, 16);
  assert.equal(result[0].fiscal_month, 16);
});

test("ページネーションで全件取得", async () => {
  // 150 件を 100/ページで 2 ページに分割
  const entries = [];
  for (let i = 1; i <= 150; i++) {
    entries.push({
      id: i, fiscal_year: 2026,
      is_closing: false, fiscal_month: (i % 12) + 1,
      encrypted_blob: null, blob_iv: null,
      lines: [],
    });
  }
  const client = makeMockClient();
  const fetchImpl = makeFetch(entries, 100);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  // 平文 description は API から撤去済のため id で順序を検証する。
  assert.equal(result.length, 150);
  assert.equal(result[0].id, 1);
  assert.equal(result[149].id, 150);
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
  // 復号失敗 + 非 closing → date=null / description="" (平文フォールバック廃止)
  assert.equal(result[0].id, 100);
  assert.equal(result[0].date, null);
  assert.equal(result[0].description, "");
});

test("Option B: encrypted line は line.id が無くても復号成功する", async () => {
  // E3-F PR-A: AAD に line.id を含めないので、id 欠落でも復号可能。
  // (旧 E3-C-1b では line.id を AAD に使っていたため throw していた)
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
  entry.lines = [lineWithoutId];
  const fetchImpl = makeFetch([entry]);
  const result = await fetchJournalsForYear({
    client, userId, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result.length, 1);
  assert.equal(result[0].description, "test entry");
  // line は復号成功
  assert.equal(result[0].lines[0].account_code, "5010");
  assert.equal(result[0].lines[0].debit, 100);
});

test("batch_id は API レスポンス平文からは取得しない (null)", async () => {
  // E3-F PR-D-6-3b: batch_id は entryBody に含まれず (batch top-level に集約)、
  // 平文フォールバックも廃止したため復号不能 entry では null になる。
  const client = makeMockClient();
  const fetchImpl = makeFetch([{
    id: 1, fiscal_year: 2026,
    is_closing: false, fiscal_month: 1,
    batch_id: "test-batch-uuid-123",
    encrypted_blob: null, blob_iv: null,
    lines: [],
  }]);
  const result = await fetchJournalsForYear({
    client, userId: 1, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result[0].batch_id, null);
});

test("fiscal_period は復号 blob から取れる (平文フォールバック廃止)", async () => {
  // E3-F PR-D-6-3b: API は平文 fiscal_period を返さない。期首振戻 (fp=0) も
  // 暗号文 (body.fiscal_period) から復元する。
  const client = makeMockClient();
  const userId = 1;
  const entry = await makeEncryptedEntry(client, userId, 1, {
    v: 1, date: "2026-01-01", description: "期首",
    source: "journal", fiscal_period: 0, fiscal_year: 2026,
  });
  const fetchImpl = makeFetch([entry]);
  const result = await fetchJournalsForYear({
    client, userId, fiscalYear: 2026, fetchImpl,
  });
  assert.equal(result[0].fiscal_period, 0);
  assert.equal(result[0].source, "journal");
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


// ============ decryptEntryMeta / fetchEntryFields (D-6-3b-3) ============

test("decryptEntryMeta: blob を復号して entry フィールドを返す", async () => {
  const client = makeMockClient();
  const userId = 7;
  const apiEntry = await makeEncryptedEntry(client, userId, 300, {
    v: 1,
    date: "2026-03-10",
    description: "事務用品",
    source: "journal",
    batch_id: null,
    fiscal_period: 3,
    fiscal_year: 2026,
  });
  const meta = await decryptEntryMeta(client, userId, apiEntry);
  assert.equal(meta.date, "2026-03-10");
  assert.equal(meta.description, "事務用品");
  assert.equal(meta.source, "journal");
  assert.equal(meta.fiscal_period, 3);
  assert.equal(meta.batch_id, null);
});

test("decryptEntryMeta: closing (blob 空) は保持列から合成する", async () => {
  const client = makeMockClient();
  const meta = await decryptEntryMeta(client, 1, {
    id: 400, fiscal_year: 2025, is_closing: true,
    encrypted_blob: null, blob_iv: null,
  });
  assert.equal(meta.date, "2025-12-31");
  assert.equal(meta.description, "損益振替仕訳（自動生成）");
  assert.equal(meta.source, "closing");
  assert.equal(meta.fiscal_period, 16);
});

test("decryptEntryMeta: 復号失敗 (別 userId) かつ非 closing は null/空", async () => {
  const client = makeMockClient();
  const apiEntry = await makeEncryptedEntry(client, 1, 500, {
    v: 1, date: "2026-04-01", description: "x", source: "journal",
    fiscal_period: 4, fiscal_year: 2026,
  });
  // 別 userId で復号 → AAD 不一致 → body=null, 非 closing
  const meta = await decryptEntryMeta(client, 999, apiEntry);
  assert.equal(meta.date, null);
  assert.equal(meta.description, "");
  assert.equal(meta.source, "");
  assert.equal(meta.fiscal_period, null);
});

test("fetchEntryFields: GET /api/v1/journals/<id> を復号して返す", async () => {
  const client = makeMockClient();
  const userId = 5;
  const apiEntry = await makeEncryptedEntry(client, userId, 600, {
    v: 1, date: "2026-06-15", description: "交通費", source: "cashbook",
    fiscal_period: 6, fiscal_year: 2026,
  });
  let calledUrl = null;
  const fetchImpl = async (url) => {
    calledUrl = url;
    return { ok: true, json: async () => ({ ok: true, journal: apiEntry }) };
  };
  const meta = await fetchEntryFields({
    client, userId, entryId: 600, fetchImpl,
  });
  assert.equal(calledUrl, "/api/v1/journals/600");
  assert.equal(meta.date, "2026-06-15");
  assert.equal(meta.description, "交通費");
});

test("fetchEntryFields: client なしで throw", async () => {
  await assert.rejects(
    () => fetchEntryFields({ userId: 1, entryId: 1 }),
    /client.*required/,
  );
});

test("fetchEntryFields: entryId なしで throw", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => fetchEntryFields({ client, userId: 1 }),
    /entryId is required/,
  );
});

test("fetchEntryFields: HTTP エラーで throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({
    ok: false, status: 404, json: async () => ({ error: "見つかりません" }),
  });
  await assert.rejects(
    () => fetchEntryFields({ client, userId: 1, entryId: 9, fetchImpl }),
    /HTTP 404/,
  );
});

test("fetchEntryFields: journal 欠落レスポンスで throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({ ok: true, json: async () => ({ ok: true }) });
  await assert.rejects(
    () => fetchEntryFields({ client, userId: 1, entryId: 9, fetchImpl }),
    /missing journal/,
  );
});


// ============ decryptLineDescriptions (D-6-5-pre2) ============

test("decryptLineDescriptions: line blob を復号して id と description を返す", async () => {
  const client = makeMockClient();
  const userId = 7;
  const l1 = await makeEncryptedLine(client, userId, 10, 100, {
    v: 1, account_code: "5010", debit_amount: 500, credit_amount: 0,
    description: "タクシー代",
  });
  const l2 = await makeEncryptedLine(client, userId, 10, 101, {
    v: 1, account_code: "1010", debit_amount: 0, credit_amount: 500,
    description: "",
  });
  const r = await decryptLineDescriptions(client, userId, [l1, l2]);
  assert.equal(r.length, 2);
  assert.deepEqual(r[0], { id: 100, description: "タクシー代" });
  assert.deepEqual(r[1], { id: 101, description: "" });
});

test("decryptLineDescriptions: blob 無し行 (集約行等) は description 空", async () => {
  const client = makeMockClient();
  const r = await decryptLineDescriptions(client, 1, [
    { id: 5, encrypted_blob: null, blob_iv: null },
  ]);
  assert.deepEqual(r, [{ id: 5, description: "" }]);
});

test("decryptLineDescriptions: 復号失敗 (別 userId) は description 空 (全体は reject せず)", async () => {
  const client = makeMockClient();
  const line = await makeEncryptedLine(client, 1, 10, 200, {
    v: 1, description: "秘密",
  });
  const r = await decryptLineDescriptions(client, 2, [line]);  // userId mismatch
  assert.deepEqual(r, [{ id: 200, description: "" }]);
});

test("fetchEntryFields: lines の行摘要も復号して返す", async () => {
  const client = makeMockClient();
  const userId = 5;
  const apiEntry = await makeEncryptedEntry(client, userId, 700, {
    v: 1, date: "2026-06-15", description: "出張", fiscal_year: 2026,
  });
  apiEntry.lines = [
    await makeEncryptedLine(client, userId, 700, 800, {
      v: 1, account_code: "5010", debit_amount: 100, credit_amount: 0,
      description: "新幹線",
    }),
  ];
  const fetchImpl = async () => ({
    ok: true, json: async () => ({ ok: true, journal: apiEntry }),
  });
  const fields = await fetchEntryFields({ client, userId, entryId: 700, fetchImpl });
  assert.equal(fields.description, "出張");
  assert.equal(fields.lines.length, 1);
  assert.deepEqual(fields.lines[0], { id: 800, description: "新幹線" });
});

// ============ fetchEntryForDiff (E5 §14.9 構造化差分の旧仕訳取得) ============

test("fetchEntryForDiff: GET /api/v1/journals/<id> を完全復号し明細科目/金額を返す", async () => {
  const client = makeMockClient();
  const userId = 8;
  const apiEntry = await makeEncryptedEntry(client, userId, 700, {
    v: 1, date: "2026-05-22", description: "携帯料金", source: "journal",
    fiscal_period: 5, fiscal_year: 2026,
  });
  apiEntry.lines = [
    await makeEncryptedLine(client, userId, 700, 70, {
      account_code: "5010", debit_amount: 5000, credit_amount: 0, description: "",
    }),
    await makeEncryptedLine(client, userId, 700, 71, {
      account_code: "1010", debit_amount: 0, credit_amount: 5000, description: "",
    }),
  ];
  let calledUrl = null;
  const fetchImpl = async (url) => {
    calledUrl = url;
    return { ok: true, json: async () => ({ ok: true, journal: apiEntry }) };
  };
  const out = await fetchEntryForDiff({ client, userId, entryId: 700, fetchImpl });
  assert.equal(calledUrl, "/api/v1/journals/700");
  assert.equal(out.date, "2026-05-22");
  assert.equal(out.description, "携帯料金");
  assert.equal(out.lines.length, 2);
  assert.equal(out.lines[0].account_code, "5010");
  assert.equal(out.lines[0].debit, 5000);
  assert.equal(out.lines[1].account_code, "1010");
  assert.equal(out.lines[1].credit, 5000);
});

test("fetchEntryForDiff: line 復号失敗は平文メタへフォールバック", async () => {
  const client = makeMockClient();
  const userId = 8;
  const apiEntry = await makeEncryptedEntry(client, userId, 701, {
    v: 1, date: "2026-05-22", description: "x", source: "journal",
    fiscal_period: 5, fiscal_year: 2026,
  });
  // 別 userId で暗号化した line → AAD 不一致で復号失敗 → 平文メタへ fallback。
  const badLine = await makeEncryptedLine(client, 999, 701, 72, {
    account_code: "5010", debit_amount: 5000, credit_amount: 0, description: "",
  });
  badLine.account_code = "5010";
  badLine.debit = 5000;
  apiEntry.lines = [badLine];
  const fetchImpl = async () => ({ ok: true, json: async () => ({ ok: true, journal: apiEntry }) });
  const out = await fetchEntryForDiff({ client, userId, entryId: 701, fetchImpl });
  assert.equal(out.lines[0].account_code, "5010");
  assert.equal(out.lines[0].debit, 5000);
});

test("fetchEntryForDiff: client なしで throw", async () => {
  await assert.rejects(() => fetchEntryForDiff({ userId: 1, entryId: 1 }), /client.*required/);
});

test("fetchEntryForDiff: entryId なしで throw", async () => {
  const client = makeMockClient();
  await assert.rejects(() => fetchEntryForDiff({ client, userId: 1 }), /entryId is required/);
});

test("fetchEntryForDiff: HTTP エラーで throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({ ok: false, status: 404, json: async () => ({ error: "見つかりません" }) });
  await assert.rejects(
    () => fetchEntryForDiff({ client, userId: 1, entryId: 9, fetchImpl }),
    /HTTP 404/,
  );
});

test("fetchEntryForDiff: journal 欠落で throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => ({ ok: true, json: async () => ({ ok: true }) });
  await assert.rejects(
    () => fetchEntryForDiff({ client, userId: 1, entryId: 9, fetchImpl }),
    /missing journal/,
  );
});
