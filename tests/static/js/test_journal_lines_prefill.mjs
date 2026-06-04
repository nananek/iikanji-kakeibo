// journal_lines_prefill.js (#338 PR3) の単体テスト。
//
// applyJournalLines (純粋な行反映) と hydrateJournalLines (fetch+復号+skip 判定) を
// DI モックで検証する。DOM/Alpine は plain object で代替する。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/crypto/journal_lines_prefill.js",
  import.meta.url,
);
const { applyJournalLines, hydrateJournalLines } = await import(MOD.href);


// --- applyJournalLines ---

test("applyJournalLines: line id で科目/金額/摘要を反映し account_name を解決", () => {
  const linesScope = {
    lines: [
      { id: 10, account_code: "", account_name: "", debit_amount: 0, credit_amount: 0, description: "" },
      { id: 11, account_code: "", account_name: "", debit_amount: 0, credit_amount: 0, description: "" },
    ],
  };
  const entryLines = [
    { id: 10, account_code: "1010", debit: 5000, credit: 0, description: "現金入金" },
    { id: 11, account_code: "4010", debit: 0, credit: 5000, description: "売上" },
  ];
  applyJournalLines({
    entryLines, linesScope,
    nameResolver: (code) => ({ "1010": "現金", "4010": "売上" }[code] || ""),
  });
  assert.equal(linesScope.lines[0].account_code, "1010");
  assert.equal(linesScope.lines[0].account_name, "現金");
  assert.equal(linesScope.lines[0].debit_amount, 5000);
  assert.equal(linesScope.lines[0].credit_amount, 0);
  assert.equal(linesScope.lines[0].description, "現金入金");
  assert.equal(linesScope.lines[1].account_code, "4010");
  assert.equal(linesScope.lines[1].account_name, "売上");
  assert.equal(linesScope.lines[1].credit_amount, 5000);
});

test("applyJournalLines: id 不一致の行は変更しない", () => {
  const linesScope = {
    lines: [{ id: 99, account_code: "keep", account_name: "K", debit_amount: 1, credit_amount: 2, description: "d" }],
  };
  applyJournalLines({
    entryLines: [{ id: 10, account_code: "1010", debit: 5000, credit: 0, description: "x" }],
    linesScope,
    nameResolver: () => "現金",
  });
  assert.equal(linesScope.lines[0].account_code, "keep");
  assert.equal(linesScope.lines[0].debit_amount, 1);
});

test("applyJournalLines: id=null の新規行はスキップ", () => {
  const linesScope = {
    lines: [{ id: null, account_code: "new", account_name: "N", debit_amount: 3, credit_amount: 0, description: "n" }],
  };
  applyJournalLines({
    entryLines: [{ id: 10, account_code: "1010", debit: 5000, credit: 0, description: "x" }],
    linesScope,
    nameResolver: () => "現金",
  });
  assert.equal(linesScope.lines[0].account_code, "new");
});

test("applyJournalLines: nameResolver 省略時は account_code のみ更新", () => {
  const linesScope = {
    lines: [{ id: 10, account_code: "", account_name: "旧名", debit_amount: 0, credit_amount: 0, description: "" }],
  };
  applyJournalLines({
    entryLines: [{ id: 10, account_code: "1010", debit: 100, credit: 0, description: "" }],
    linesScope,
  });
  assert.equal(linesScope.lines[0].account_code, "1010");
  // nameResolver 無しなので account_name は不変
  assert.equal(linesScope.lines[0].account_name, "旧名");
});

test("applyJournalLines: account_code が空なら nameResolver を呼ばず name 不変", () => {
  let called = false;
  const linesScope = {
    lines: [{ id: 10, account_code: "x", account_name: "旧", debit_amount: 0, credit_amount: 0, description: "" }],
  };
  applyJournalLines({
    entryLines: [{ id: 10, account_code: "", debit: 0, credit: 0, description: "" }],
    linesScope,
    nameResolver: () => { called = true; return "新"; },
  });
  assert.equal(linesScope.lines[0].account_code, "");
  assert.equal(called, false);
  assert.equal(linesScope.lines[0].account_name, "旧");
});

test("applyJournalLines: linesScope が無ければ何もしない", () => {
  // 例外を投げないこと
  applyJournalLines({ entryLines: [{ id: 1, account_code: "1010", debit: 1, credit: 0 }], linesScope: null });
  applyJournalLines({ entryLines: [], linesScope: { lines: null } });
});

test("applyJournalLines: entryLines が配列でなければ何もしない", () => {
  const linesScope = { lines: [{ id: 10, account_code: "keep", account_name: "", debit_amount: 0, credit_amount: 0, description: "" }] };
  applyJournalLines({ entryLines: null, linesScope });
  assert.equal(linesScope.lines[0].account_code, "keep");
});


// --- hydrateJournalLines ---

function makeClientClass(hasKey) {
  let closed = false;
  class C {
    constructor(_url) {}
    async status() { return { hasKey }; }
    async decrypt() { return {}; }
    close() { closed = true; }
  }
  C._wasClosed = () => closed;
  return C;
}

test("hydrateJournalLines: 新規 (isEdit=false) は skip", async () => {
  const r = await hydrateJournalLines({ isEdit: false, entryId: 1, userId: 1 });
  assert.equal(r, null);
});

test("hydrateJournalLines: entryId 欠落は skip", async () => {
  const r = await hydrateJournalLines({ isEdit: true, entryId: null, userId: 1 });
  assert.equal(r, null);
});

test("hydrateJournalLines: MK ロック中 (hasKey=false) は復号せず null", async () => {
  const ClientClass = makeClientClass(false);
  let fetched = false;
  const r = await hydrateJournalLines({
    isEdit: true, entryId: 1, userId: 1,
    ClientClass,
    fetchEntry: async () => { fetched = true; return { lines: [] }; },
  });
  assert.equal(r, null);
  assert.equal(fetched, false);
  assert.equal(ClientClass._wasClosed(), true);
});

test("hydrateJournalLines: MK 解除済なら fetch+反映して entryLines を返す", async () => {
  const ClientClass = makeClientClass(true);
  const linesScope = {
    lines: [
      { id: 10, account_code: "", account_name: "", debit_amount: 0, credit_amount: 0, description: "" },
    ],
  };
  const formEl = { _tag: "form" };
  const alpine = { $data: (el) => (el === formEl ? linesScope : null) };
  const entry = { lines: [{ id: 10, account_code: "1010", debit: 700, credit: 0, description: "メモ" }] };
  const r = await hydrateJournalLines({
    isEdit: true, entryId: 42, userId: 9,
    formEl, alpine,
    nameResolver: () => "現金",
    ClientClass,
    fetchEntry: async ({ entryId, userId }) => {
      assert.equal(entryId, 42);
      assert.equal(userId, 9);
      return entry;
    },
  });
  assert.deepEqual(r, entry.lines);
  assert.equal(linesScope.lines[0].account_code, "1010");
  assert.equal(linesScope.lines[0].account_name, "現金");
  assert.equal(linesScope.lines[0].debit_amount, 700);
  assert.equal(linesScope.lines[0].description, "メモ");
  assert.equal(ClientClass._wasClosed(), true);
});

test("hydrateJournalLines: formEl/alpine 無しでも例外を投げず entryLines を返す", async () => {
  const ClientClass = makeClientClass(true);
  const entry = { lines: [{ id: 1, account_code: "1010", debit: 1, credit: 0, description: "" }] };
  const r = await hydrateJournalLines({
    isEdit: true, entryId: 1, userId: 1,
    ClientClass,
    fetchEntry: async () => entry,
  });
  assert.deepEqual(r, entry.lines);
});
