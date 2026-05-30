// cashbook/index_renderer.buildCashbookRows の Node 単体テスト。
//
// 出納帳一覧のクライアント描画 (E3-F PR-D-4-2) の行生成ロジックを検証する。
// 旧サーバ実装 (order_by(date desc, entry_number desc) / source="cashbook"
// フィルタ / 借方貸方科目名・金額) の振る舞いを移植。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/cashbook/index_renderer.mjs",
  import.meta.url,
);
const { buildCashbookRows } = await import(MOD.href);


const META = {
  "5010": { name: "食費" },
  "5020": { name: "交際費" },
  "1010": { name: "現金" },
  "1020": { name: "普通預金" },
};

function entry({ id, entry_number, date, description = "", source = "cashbook", lines }) {
  return { id, entry_number, date, description, source, lines };
}

// 通常の出金: 借方 費目 / 貸方 支払元
function expense({ id, entry_number, date, amount, catCode = "5010", payCode = "1010", description = "" }) {
  return entry({
    id, entry_number, date, description, source: "cashbook",
    lines: [
      { account_code: catCode, debit: amount, credit: 0 },
      { account_code: payCode, debit: 0, credit: amount },
    ],
  });
}


test("空 journals は [] を返す", () => {
  assert.deepEqual(buildCashbookRows([], META), []);
  assert.deepEqual(buildCashbookRows(null, META), []);
});

test("source!=cashbook の仕訳は除外する", () => {
  const journals = [
    expense({ id: 1, entry_number: 1, date: "2026-02-15", amount: 1000 }),
    entry({
      id: 2, entry_number: 2, date: "2026-02-16", source: "journal",
      lines: [{ account_code: "5010", debit: 500, credit: 0 }],
    }),
  ];
  const rows = buildCashbookRows(journals, META);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].entry_id, 1);
});

test("借方/貸方科目名を meta で解決・金額は借方合計", () => {
  const rows = buildCashbookRows(
    [expense({ id: 1, entry_number: 1, date: "2026-02-15", amount: 1200, catCode: "5010", payCode: "1020" })],
    META,
  );
  assert.deepEqual(rows[0].debit_names, ["食費"]);
  assert.deepEqual(rows[0].credit_names, ["普通預金"]);
  assert.equal(rows[0].amount, 1200);
});

test("meta に無い科目はコードにフォールバック", () => {
  const rows = buildCashbookRows(
    [expense({ id: 1, entry_number: 1, date: "2026-02-15", amount: 100, catCode: "9999" })],
    META,
  );
  assert.deepEqual(rows[0].debit_names, ["9999"]);
});

test("date 降順・同日は entry_number 降順でソート", () => {
  const journals = [
    expense({ id: 1, entry_number: 1, date: "2026-02-15", amount: 100 }),
    expense({ id: 2, entry_number: 2, date: "2026-02-16", amount: 200 }),
    expense({ id: 3, entry_number: 3, date: "2026-02-15", amount: 300 }),
  ];
  const rows = buildCashbookRows(journals, META);
  assert.deepEqual(rows.map((r) => r.entry_id), [2, 3, 1]);
});

test("date 欠落は最後尾に送られる", () => {
  const journals = [
    entry({
      id: 1, entry_number: 1, date: null, source: "cashbook",
      lines: [{ account_code: "5010", debit: 50, credit: 0 }, { account_code: "1010", debit: 0, credit: 50 }],
    }),
    expense({ id: 2, entry_number: 2, date: "2026-01-01", amount: 100 }),
  ];
  const rows = buildCashbookRows(journals, META);
  assert.deepEqual(rows.map((r) => r.entry_id), [2, 1]);
});

test("複数の借方/貸方明細を全て収集", () => {
  const e = entry({
    id: 1, entry_number: 1, date: "2026-03-01", source: "cashbook",
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "5020", debit: 200, credit: 0 },
      { account_code: "1010", debit: 0, credit: 300 },
    ],
  });
  const rows = buildCashbookRows([e], META);
  assert.deepEqual(rows[0].debit_names, ["食費", "交際費"]);
  assert.deepEqual(rows[0].credit_names, ["現金"]);
  assert.equal(rows[0].amount, 300);
});

test("entry_number / description を保持", () => {
  const rows = buildCashbookRows(
    [expense({ id: 7, entry_number: 42, date: "2026-02-15", amount: 100, description: "コンビニ" })],
    META,
  );
  assert.equal(rows[0].entry_number, 42);
  assert.equal(rows[0].description, "コンビニ");
});
