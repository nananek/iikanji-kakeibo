// closing.js (#338 item1) の単体テスト。
//
// buildClosingLines (純粋な損益振替行の構築) を全分岐で検証し、
// buildAndPostClosingEntry (集計→暗号化→POST) を DI モックで検証する。
// entries_builder の fiscalPeriod=16 許可 (_allowClosing) の回帰も確認する。

import { test } from "node:test";
import assert from "node:assert/strict";

const CLOSING = new URL(
  "../../../app/static/js/crypto/reports/closing.js",
  import.meta.url,
);
const { buildClosingLines, buildAndPostClosingEntry } = await import(CLOSING.href);

const BUILDER = new URL(
  "../../../app/static/js/crypto/entries_builder.js",
  import.meta.url,
);
const { buildJournalEntry } = await import(BUILDER.href);


const META = {
  "4010": { type: "revenue" },
  "4020": { type: "revenue" },
  "5010": { type: "expense" },
  "5020": { type: "expense" },
  "1010": { type: "asset" },
  "3020": { type: "equity", system_role: "retained_earnings" },
};

function sumSides(lines) {
  let d = 0, c = 0;
  for (const l of lines) { d += l.debit; c += l.credit; }
  return [d, c];
}


// --- buildClosingLines ---

test("buildClosingLines: 収益のみ (利益) → 収益を借方戻し + 繰越利益へ貸方", () => {
  // 4010 貸方残 1000 (credit 1000)
  const lines = buildClosingLines({ "4010": [0, 1000] }, META);
  assert.deepEqual(lines, [
    { account_code: "4010", debit: 1000, credit: 0 },
    { account_code: "3020", debit: 0, credit: 1000 },
  ]);
  assert.deepEqual(sumSides(lines), [1000, 1000]);  // 貸借一致
});

test("buildClosingLines: 費用のみ (損失) → 費用を貸方戻し + 繰越利益へ借方", () => {
  // 5010 借方残 500 (debit 500)
  const lines = buildClosingLines({ "5010": [500, 0] }, META);
  assert.deepEqual(lines, [
    { account_code: "5010", debit: 0, credit: 500 },
    { account_code: "3020", debit: 500, credit: 0 },
  ]);
  assert.deepEqual(sumSides(lines), [500, 500]);
});

test("buildClosingLines: 収益>費用 (利益) → 繰越利益は貸方", () => {
  const lines = buildClosingLines(
    { "4010": [0, 1000], "5010": [600, 0] }, META,
  );
  // 4010 借方1000 / 5010 貸方600 / 3020 貸方400
  assert.deepEqual(lines, [
    { account_code: "4010", debit: 1000, credit: 0 },
    { account_code: "5010", debit: 0, credit: 600 },
    { account_code: "3020", debit: 0, credit: 400 },
  ]);
  assert.deepEqual(sumSides(lines), [1000, 1000]);
});

test("buildClosingLines: 費用>収益 (損失) → 繰越利益は借方", () => {
  const lines = buildClosingLines(
    { "4010": [0, 600], "5010": [1000, 0] }, META,
  );
  assert.deepEqual(lines, [
    { account_code: "4010", debit: 600, credit: 0 },
    { account_code: "5010", debit: 0, credit: 1000 },
    { account_code: "3020", debit: 400, credit: 0 },
  ]);
  assert.deepEqual(sumSides(lines), [1000, 1000]);
});

test("buildClosingLines: 収益=費用 (net 0) → 繰越利益行なし・貸借一致", () => {
  const lines = buildClosingLines(
    { "4010": [0, 500], "5010": [500, 0] }, META,
  );
  assert.equal(lines.length, 2);
  assert.ok(!lines.some((l) => l.account_code === "3020"));
  assert.deepEqual(sumSides(lines), [500, 500]);
});

test("buildClosingLines: 残高ゼロの科目はスキップ → 全てゼロなら空配列", () => {
  // 4010 は debit==credit で残高 0
  const lines = buildClosingLines({ "4010": [1000, 1000] }, META);
  assert.deepEqual(lines, []);
});

test("buildClosingLines: 負の収益残高 (借方超過) は貸方戻し + 符号反転", () => {
  // 4010 借方残 300 (返金等で収益が借方超過) → bal = 0-300 = -300
  const lines = buildClosingLines({ "4010": [300, 0] }, META);
  assert.deepEqual(lines, [
    { account_code: "4010", debit: 0, credit: 300 },
    { account_code: "3020", debit: 300, credit: 0 },  // net=-300 → 借方
  ]);
  assert.deepEqual(sumSides(lines), [300, 300]);
});

test("buildClosingLines: 負の費用残高 (貸方超過) は借方戻し + 符号反転", () => {
  // 5010 貸方残 200 (費用の戻し等で貸方超過) → bal = debit-credit = -200
  const lines = buildClosingLines({ "5010": [0, 200] }, META);
  assert.deepEqual(lines, [
    { account_code: "5010", debit: 200, credit: 0 },
    { account_code: "3020", debit: 0, credit: 200 },  // net = 0-(-200) = 200 → 貸方
  ]);
  assert.deepEqual(sumSides(lines), [200, 200]);
});

test("buildClosingLines: 複数科目を集計", () => {
  const lines = buildClosingLines(
    { "4010": [0, 800], "4020": [0, 200], "5010": [300, 0], "5020": [100, 0] },
    META,
  );
  // 収益計 1000 / 費用計 400 / net 600
  assert.deepEqual(sumSides(lines), [1000, 1000]);
  const retained = lines.find((l) => l.account_code === "3020");
  assert.deepEqual(retained, { account_code: "3020", debit: 0, credit: 600 });
});

test("buildClosingLines: 収益費用以外の科目 (資産) は無視", () => {
  const lines = buildClosingLines(
    { "1010": [5000, 0], "4010": [0, 1000] }, META,
  );
  assert.ok(!lines.some((l) => l.account_code === "1010"));
  assert.deepEqual(sumSides(lines), [1000, 1000]);
});

test("buildClosingLines: 空 balanceCache → 空配列", () => {
  assert.deepEqual(buildClosingLines({}, META), []);
});

test("buildClosingLines: 活動ありなのに繰越利益科目欠落 → throw", () => {
  const metaNoRetained = { "4010": { type: "revenue" } };
  assert.throws(
    () => buildClosingLines({ "4010": [0, 1000] }, metaNoRetained),
    /繰越利益/,
  );
});

test("buildClosingLines: 未知コードはスキップ (meta に無い)", () => {
  const lines = buildClosingLines({ "9999": [0, 1000], "4010": [0, 500] }, META);
  assert.ok(!lines.some((l) => l.account_code === "9999"));
  assert.deepEqual(sumSides(lines), [500, 500]);
});


// --- buildAndPostClosingEntry ---

// computeBalanceCache が受け取る形 (fiscal_month / is_closing / lines)。
function entry({ fp = 5, closing = false, lines = [] }) {
  return { fiscal_month: fp, is_closing: closing, lines };
}

test("buildAndPostClosingEntry: 集計→buildJournalEntry→POST で closing_entry を送る", async () => {
  const entries = [
    entry({ fp: 5, lines: [
      { account_code: "1010", debit: 0, credit: 1000 },
      { account_code: "4010", debit: 0, credit: 1000 },  // 収益貸方1000
    ] }),
    // 上の行は二重計上にならないよう実際の仕訳に合わせる: 現金/売上
    entry({ fp: 5, lines: [
      { account_code: "1010", debit: 1000, credit: 0 },
    ] }),
  ];
  let postedBody = null;
  const res = await buildAndPostClosingEntry({
    client: {}, userId: 7, year: 2026, accountsMeta: META,
    fetchJournals: async ({ fiscalYear }) => {
      assert.equal(fiscalYear, 2026);
      return entries;
    },
    buildImpl: async ({ fiscalPeriod, _allowClosing, lines, date }) => {
      assert.equal(fiscalPeriod, 16);
      assert.equal(_allowClosing, true);
      assert.equal(date, "2026-12-31");
      return { fiscal_year: 2026, fiscal_month: 16, encrypted_blob: "BLOB",
               blob_iv: "IV", lines };
    },
    postImpl: async (url, opts) => {
      assert.equal(url, "/api/v1/fiscal/close-closing");
      postedBody = JSON.parse(opts.body);
      return { ok: true, json: async () => ({ ok: true, closed_period: 15, closing_entry_id: 1 }) };
    },
  });
  assert.equal(res.closed_period, 15);
  assert.equal(postedBody.year, 2026);
  assert.equal(postedBody.closing_entry.encrypted_blob, "BLOB");
});

test("buildAndPostClosingEntry: 収益費用ゼロなら closing_entry=null で POST", async () => {
  let postedBody = null;
  let buildCalled = false;
  await buildAndPostClosingEntry({
    client: {}, userId: 7, year: 2026, accountsMeta: META,
    fetchJournals: async () => [
      entry({ fp: 3, lines: [
        { account_code: "1010", debit: 500, credit: 0 },
        { account_code: "1020", debit: 0, credit: 500 },  // 資産間振替 (収益費用なし)
      ] }),
    ],
    buildImpl: async () => { buildCalled = true; return {}; },
    postImpl: async (url, opts) => {
      postedBody = JSON.parse(opts.body);
      return { ok: true, json: async () => ({ ok: true }) };
    },
  });
  assert.equal(buildCalled, false);
  assert.equal(postedBody.closing_entry, null);
});

test("buildAndPostClosingEntry: サーバ 4xx は error message を throw", async () => {
  await assert.rejects(
    buildAndPostClosingEntry({
      client: {}, userId: 1, year: 2026, accountsMeta: META,
      fetchJournals: async () => [],
      postImpl: async () => ({
        ok: false, status: 409,
        json: async () => ({ error: "決算月2までを確定してください。" }),
      }),
    }),
    /決算月2/,
  );
});


// --- entries_builder fiscalPeriod=16 許可 (回帰) ---

test("buildJournalEntry: 通常経路は fiscalPeriod=16 を throw", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-12-31",
    lines: [
      { account_code: "4010", debit: 1000, credit: 0 },
      { account_code: "3020", debit: 0, credit: 1000 },
    ],
    fiscalPeriod: 16,
  }), /fiscalPeriod/);
});

test("buildJournalEntry: _allowClosing で fiscalPeriod=16 を許可", () => {
  const e = buildJournalEntry({
    date: "2026-12-31",
    lines: [
      { account_code: "4010", debit: 1000, credit: 0 },
      { account_code: "3020", debit: 0, credit: 1000 },
    ],
    fiscalPeriod: 16,
    _allowClosing: true,
  });
  // client 未指定なので平文 entry を返す (fiscal_period=16)
  assert.equal(e.fiscal_period, 16);
});

test("buildJournalEntry: _allowClosing でも 17 は throw", () => {
  assert.throws(() => buildJournalEntry({
    date: "2026-12-31",
    lines: [
      { account_code: "4010", debit: 1000, credit: 0 },
      { account_code: "3020", debit: 0, credit: 1000 },
    ],
    fiscalPeriod: 17,
    _allowClosing: true,
  }), /fiscalPeriod/);
});
