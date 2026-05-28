// Tests for client-side accounting helpers (buildCashbookEntry / buildTransferEntry).

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/entries_builder.js",
  import.meta.url,
);
const { buildCashbookEntry, buildJournalEntry, buildTransferEntry } = await import(M.href);

const REC = new URL(
  "../../../app/static/js/crypto/record.js",
  import.meta.url,
);
const { buildAAD, decryptRecord } = await import(REC.href);

const B64 = new URL(
  "../../../app/static/js/crypto/b64.js",
  import.meta.url,
);
const { b64decode } = await import(B64.href);


// --- mock SharedCryptoClient (test_record.mjs と同パターン) ---
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


// --- buildCashbookEntry ---


test("expense: 費目=借方 / 支払口座=貸方", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    description: "ランチ",
    transactionType: "expense",
    paymentAccountCode: "1010",
    categoryAccountCode: "5010",
    amount: 800,
  });
  assert.equal(e.date, "2026-02-15");
  assert.equal(e.description, "ランチ");
  assert.equal(e.source, "cashbook");
  assert.equal(e.fiscal_period, null);
  assert.deepEqual(e.lines, [
    { account_code: "5010", debit: 800, credit: 0 },
    { account_code: "1010", debit: 0, credit: 800 },
  ]);
});

test("income: 入金口座=借方 / 収益科目=貸方", () => {
  const e = buildCashbookEntry({
    date: "2026-02-25",
    description: "給与",
    transactionType: "income",
    paymentAccountCode: "1020",
    categoryAccountCode: "4010",
    amount: 300000,
  });
  assert.deepEqual(e.lines, [
    { account_code: "1020", debit: 300000, credit: 0 },
    { account_code: "4010", debit: 0, credit: 300000 },
  ]);
});

test("負金額 (expense): 借方・貸方が入れ替わる (返金処理)", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    description: "返金",
    transactionType: "expense",
    paymentAccountCode: "1010",
    categoryAccountCode: "5010",
    amount: -500,
  });
  // 通常 expense は 5010=debit / 1010=credit、負だと逆 = 1010=debit / 5010=credit
  assert.deepEqual(e.lines, [
    { account_code: "1010", debit: 500, credit: 0 },
    { account_code: "5010", debit: 0, credit: 500 },
  ]);
});

test("source / fiscalPeriod を上書きできる (csv 取込等)", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010",
    categoryAccountCode: "5010",
    amount: 100,
    source: "csv",
    fiscalPeriod: 2,
  });
  assert.equal(e.source, "csv");
  assert.equal(e.fiscal_period, 2);
});

test("不正な transactionType で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "invalid",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  }), TypeError);
});

test("amount=0 で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 0,
  }), TypeError);
});

test("float amount で TypeError (黙って丸めない)", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100.5,
  }), TypeError);
});

test("payment / category account_code 欠落で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "", categoryAccountCode: "5010", amount: 100,
  }), TypeError);
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "", amount: 100,
  }), TypeError);
});


// --- buildTransferEntry ---


test("transfer: to=借方 / from=貸方", () => {
  const e = buildTransferEntry({
    date: "2026-02-20",
    description: "現金→普通預金",
    fromAccountCode: "1010",
    toAccountCode: "1020",
    amount: 50000,
  });
  assert.deepEqual(e.lines, [
    { account_code: "1020", debit: 50000, credit: 0 },
    { account_code: "1010", debit: 0, credit: 50000 },
  ]);
});

test("transfer: 負金額で from/to が反転 (打消し)", () => {
  const e = buildTransferEntry({
    date: "2026-02-20",
    description: "取消",
    fromAccountCode: "1010",
    toAccountCode: "1020",
    amount: -50000,
  });
  assert.deepEqual(e.lines, [
    { account_code: "1010", debit: 50000, credit: 0 },
    { account_code: "1020", debit: 0, credit: 50000 },
  ]);
});

test("transfer: 同一 from/to で TypeError", () => {
  assert.throws(() => buildTransferEntry({
    date: "2026-02-20", description: "x",
    fromAccountCode: "1010", toAccountCode: "1010", amount: 100,
  }), TypeError);
});

test("transfer: amount=0 で TypeError", () => {
  assert.throws(() => buildTransferEntry({
    date: "2026-02-20", description: "x",
    fromAccountCode: "1010", toAccountCode: "1020", amount: 0,
  }), TypeError);
});

test("date 欠落で TypeError (cashbook / transfer 両方)", () => {
  assert.throws(() => buildCashbookEntry({
    description: "x", transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  }), TypeError);
  assert.throws(() => buildTransferEntry({
    description: "x", fromAccountCode: "1010", toAccountCode: "1020", amount: 100,
  }), TypeError);
});

test("fiscalPeriod=16 (損益振替) は TypeError (cashbook / transfer 両方)", () => {
  // サーバ batch API も拒否するが、クライアントでも fail-loud で早期検知
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
    fiscalPeriod: 16,
  }), TypeError);
  assert.throws(() => buildTransferEntry({
    date: "2026-02-20", description: "x",
    fromAccountCode: "1010", toAccountCode: "1020", amount: 100,
    fiscalPeriod: 16,
  }), TypeError);
});

test("fiscalPeriod が範囲外 / 非整数で TypeError", () => {
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
    fiscalPeriod: -1,
  }), TypeError);
  assert.throws(() => buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
    fiscalPeriod: 1.5,
  }), TypeError);
});

test("description 省略時は空文字", () => {
  const e = buildCashbookEntry({
    date: "2026-02-15",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  });
  assert.equal(e.description, "");
});


// --- E3-F PR-A: client + userId 指定時の暗号化対応 ---


test("encrypted: client + userId 指定で encrypted_blob / blob_iv / fiscal_year が付く", async () => {
  const client = makeMockClient();
  const e = await buildCashbookEntry({
    client, userId: 1,
    date: "2026-05-22", description: "スーパー",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 800,
  });
  assert.equal(e.fiscal_year, 2026);
  assert.ok(typeof e.encrypted_blob === "string" && e.encrypted_blob.length > 0);
  assert.ok(typeof e.blob_iv === "string" && e.blob_iv.length > 0);
  assert.equal(e.lines.length, 2);
  for (const line of e.lines) {
    assert.ok(typeof line.encrypted_blob === "string" && line.encrypted_blob.length > 0);
    assert.ok(typeof line.blob_iv === "string" && line.blob_iv.length > 0);
    // 旧平文フィールド (dual-storage) も併送
    assert.ok(typeof line.account_code === "string");
    assert.ok(Number.isInteger(line.debit));
    assert.ok(Number.isInteger(line.credit));
  }
});


test("encrypted: 暗号文 + AAD で復号すると元の body が戻る (round-trip)", async () => {
  const client = makeMockClient();
  const userId = 7;
  const e = await buildCashbookEntry({
    client, userId,
    date: "2026-03-01", description: "テスト摘要",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 1234,
    source: "csv", fiscalPeriod: 3,
  });
  // entry 本体の暗号文を復号
  const entryBody = await decryptRecord(
    client,
    b64decode(e.encrypted_blob),
    b64decode(e.blob_iv),
    buildAAD("je", userId),
  );
  assert.equal(entryBody.v, 1);
  assert.equal(entryBody.date, "2026-03-01");
  assert.equal(entryBody.description, "テスト摘要");
  assert.equal(entryBody.source, "csv");
  assert.equal(entryBody.fiscal_period, 3);
  // 各 line も復号
  for (const line of e.lines) {
    const lineBody = await decryptRecord(
      client,
      b64decode(line.encrypted_blob),
      b64decode(line.blob_iv),
      buildAAD("jel", userId),
    );
    assert.equal(lineBody.v, 1);
    assert.equal(lineBody.account_code, line.account_code);
    assert.equal(lineBody.debit_amount, line.debit);
    assert.equal(lineBody.credit_amount, line.credit);
  }
});


test("encrypted: 異なる userId だと復号できない (AAD すり替え)", async () => {
  const client = makeMockClient();
  const e = await buildCashbookEntry({
    client, userId: 1,
    date: "2026-03-01", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  });
  await assert.rejects(
    () => decryptRecord(
      client,
      b64decode(e.encrypted_blob),
      b64decode(e.blob_iv),
      buildAAD("je", 2),
    ),
    /AAD mismatch/,
  );
});


test("encrypted: client 指定で userId 欠落だと TypeError (validation は同期 throw)", () => {
  const client = makeMockClient();
  // userId validation は同期で走るため throw を使う (Promise reject ではない)
  assert.throws(
    () => buildCashbookEntry({
      client,
      date: "2026-03-01", description: "x",
      transactionType: "expense",
      paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
    }),
    /userId must be a number or bigint/,
  );
});


test("encrypted: buildTransferEntry も同様に暗号化される", async () => {
  const client = makeMockClient();
  const userId = 42;
  const e = await buildTransferEntry({
    client, userId,
    date: "2026-04-15", description: "口座振替",
    fromAccountCode: "1010", toAccountCode: "1020", amount: 50000,
  });
  assert.equal(e.fiscal_year, 2026);
  assert.ok(e.encrypted_blob);
  assert.ok(e.blob_iv);
  // 復号
  const body = await decryptRecord(
    client,
    b64decode(e.encrypted_blob),
    b64decode(e.blob_iv),
    buildAAD("je", userId),
  );
  assert.equal(body.date, "2026-04-15");
});


test("不正な日付 (fiscal_year 抽出失敗) で TypeError", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => buildCashbookEntry({
      client, userId: 1,
      date: "not-a-date",
      transactionType: "expense",
      paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
    }),
    /cannot derive fiscal_year/,
  );
});


// --- buildJournalEntry (E3-F PR-B2) ---


test("journal: 2 行の balanced lines をそのまま受け取り平文 entry を返す", () => {
  const e = buildJournalEntry({
    date: "2026-02-15",
    description: "テスト仕訳",
    lines: [
      { account_code: "5010", debit: 1000, credit: 0, description: "" },
      { account_code: "1010", debit: 0, credit: 1000, description: "" },
    ],
  });
  assert.equal(e.date, "2026-02-15");
  assert.equal(e.description, "テスト仕訳");
  assert.equal(e.source, "journal");
  assert.equal(e.fiscal_period, null);
  assert.deepEqual(e.lines, [
    { account_code: "5010", debit: 1000, credit: 0, description: "" },
    { account_code: "1010", debit: 0, credit: 1000, description: "" },
  ]);
});

test("journal: 3 行以上の lines もサポート (複合仕訳)", () => {
  const e = buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 700, credit: 0 },
      { account_code: "5020", debit: 300, credit: 0 },
      { account_code: "1010", debit: 0, credit: 1000 },
    ],
  });
  assert.equal(e.lines.length, 3);
  assert.equal(e.lines[1].account_code, "5020");
});

test("journal: 貸借不一致で TypeError", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 1000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 500 },
    ],
  }), /unbalanced/);
});

test("journal: lines が空 / 1 行で TypeError", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x", lines: [],
  }), /length >= 2/);
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [{ account_code: "5010", debit: 100, credit: 0 }],
  }), /length >= 2/);
});

test("journal: debit/credit 両方非ゼロは TypeError (片側のみ原則)", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 100, credit: 50 },
      { account_code: "1010", debit: 0, credit: 50 },
    ],
  }), /exactly one of debit\/credit/);
});

test("journal: debit/credit 両方 0 は TypeError (空行)", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 0, credit: 0 },
      { account_code: "1010", debit: 0, credit: 0 },
    ],
  }), /exactly one of debit\/credit/);
});

test("journal: account_code 欠落で TypeError", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "", debit: 1000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 1000 },
    ],
  }), /account_code/);
});

test("journal: 負値の debit/credit で TypeError", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: -100, credit: 0 },
      { account_code: "1010", debit: 0, credit: -100 },
    ],
  }), /non-negative integer/);
});

test("journal: float の debit/credit で TypeError", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 100.5, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
  }), /non-negative integer/);
});

test("journal: source / fiscalPeriod 上書き", () => {
  const e = buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
    source: "ai_receipt",
    fiscalPeriod: 0,
  });
  assert.equal(e.source, "ai_receipt");
  assert.equal(e.fiscal_period, 0);
});

test("journal: fiscalPeriod=16 (損益振替) で TypeError", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
    fiscalPeriod: 16,
  }), TypeError);
});

test("journal: client + userId 指定で encrypted_blob / blob_iv / fiscal_year が付く", async () => {
  const client = makeMockClient();
  const userId = 5;
  const e = await buildJournalEntry({
    client, userId,
    date: "2026-02-15", description: "暗号化仕訳",
    lines: [
      { account_code: "5010", debit: 1000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 1000 },
    ],
  });
  assert.equal(e.fiscal_year, 2026);
  assert.ok(typeof e.encrypted_blob === "string" && e.encrypted_blob.length > 0);
  assert.ok(typeof e.blob_iv === "string" && e.blob_iv.length > 0);
  for (const line of e.lines) {
    assert.ok(typeof line.encrypted_blob === "string");
    assert.ok(typeof line.blob_iv === "string");
  }
  // round-trip decrypt
  const body = await decryptRecord(
    client,
    b64decode(e.encrypted_blob),
    b64decode(e.blob_iv),
    buildAAD("je", userId),
  );
  assert.equal(body.description, "暗号化仕訳");
  assert.equal(body.source, "journal");
});

test("journal: client なしの場合は同期返却 (Promise でない)", () => {
  const e = buildJournalEntry({
    date: "2026-02-15", description: "x",
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
  });
  assert.equal(typeof e.then, "undefined");
  assert.equal(e.encrypted_blob, undefined);
});


test("client なしの場合は従来通り平文 entry を同期返却 (後方互換)", () => {
  // Promise でなく即値を返すことを確認 (await なしでも .lines にアクセスできる)
  const e = buildCashbookEntry({
    date: "2026-02-15", description: "x",
    transactionType: "expense",
    paymentAccountCode: "1010", categoryAccountCode: "5010", amount: 100,
  });
  // 戻り値は Promise ではない (平文 entry)
  assert.equal(typeof e.then, "undefined");
  assert.equal(e.encrypted_blob, undefined);
  assert.equal(e.fiscal_year, undefined);
  assert.equal(e.lines.length, 2);
});
