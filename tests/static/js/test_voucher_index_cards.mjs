// vouchers/index_renderer.buildVoucherCards / filterVoucherCards の Node 単体テスト。
//
// 証憑一覧のクライアント描画 (E3-F PR-D-4-4) のカード生成・電帳法フィルタ
// (日付/金額/摘要) を検証する。旧サーバ実装 (outerjoin + coalesce sort +
// date/amount/description filter) の振る舞いを移植。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/vouchers/index_renderer.mjs",
  import.meta.url,
);
const { buildVoucherCards, filterVoucherCards } = await import(MOD.href);


function meta(o) {
  return {
    id: o.id,
    journal_entry_id: o.journal_entry_id ?? null,
    entry_number: o.entry_number ?? null,
    fiscal_year: o.fiscal_year ?? null,
    uploaded_at: o.uploaded_at ?? null,
    has_hash: o.has_hash ?? false,
  };
}

function entryMap(obj) {
  return new Map(Object.entries(obj).map(([k, v]) => [Number(k), v]));
}


test("紐付け仕訳から date/description/amount を補完", () => {
  const cards = buildVoucherCards(
    [meta({ id: 1, journal_entry_id: 10, entry_number: 5, fiscal_year: 2026, uploaded_at: "2026-02-20T10:00:00", has_hash: true })],
    entryMap({ 10: { date: "2026-02-15", description: "コンビニ", amount: 1200, entry_number: 5 } }),
  );
  assert.equal(cards.length, 1);
  const c = cards[0];
  assert.equal(c.attached, true);
  assert.equal(c.entry_date, "2026-02-15");
  assert.equal(c.effective_date, "2026-02-15");
  assert.equal(c.description, "コンビニ");
  assert.equal(c.amount, 1200);
  assert.equal(c.entry_number, 5);
  assert.equal(c.has_hash, true);
});

test("未紐付け証憑は uploaded_at を effective_date に・amount/description なし", () => {
  const cards = buildVoucherCards(
    [meta({ id: 2, journal_entry_id: null, uploaded_at: "2026-03-01T12:30:00" })],
    new Map(),
  );
  const c = cards[0];
  assert.equal(c.attached, false);
  assert.equal(c.effective_date, "2026-03-01");
  assert.equal(c.amount, null);
  assert.equal(c.description, "");
});

test("紐付けだが仕訳未復号 (entryMap に無い) は uploaded_at fallback・amount null", () => {
  const cards = buildVoucherCards(
    [meta({ id: 3, journal_entry_id: 99, entry_number: 7, fiscal_year: 2026, uploaded_at: "2026-04-01T09:00:00" })],
    new Map(),
  );
  const c = cards[0];
  assert.equal(c.attached, true);
  assert.equal(c.entry_number, 7); // メタから取れる
  assert.equal(c.effective_date, "2026-04-01"); // uploaded fallback
  assert.equal(c.amount, null);
});

test("入力期限超過 (uploaded - 仕訳日 > 67日) で overdue_days を設定", () => {
  const cards = buildVoucherCards(
    [meta({ id: 1, journal_entry_id: 10, fiscal_year: 2026, uploaded_at: "2026-05-01T00:00:00" })],
    entryMap({ 10: { date: "2026-01-01", description: "x", amount: 1 } }),
  );
  assert.equal(cards[0].overdue_days, 120);
});

test("67日以内は overdue_days=null", () => {
  const cards = buildVoucherCards(
    [meta({ id: 1, journal_entry_id: 10, fiscal_year: 2026, uploaded_at: "2026-02-01T00:00:00" })],
    entryMap({ 10: { date: "2026-01-15", description: "x", amount: 1 } }),
  );
  assert.equal(cards[0].overdue_days, null);
});

test("effective_date 降順・同日は voucher_id 降順でソート", () => {
  const cards = buildVoucherCards([
    meta({ id: 1, journal_entry_id: 10, fiscal_year: 2026, uploaded_at: "2026-02-10T00:00:00" }),
    meta({ id: 2, journal_entry_id: null, uploaded_at: "2026-02-16T00:00:00" }),
    meta({ id: 3, journal_entry_id: 30, fiscal_year: 2026, uploaded_at: "2026-02-10T00:00:00" }),
  ], entryMap({
    10: { date: "2026-02-15", description: "a", amount: 1 },
    30: { date: "2026-02-15", description: "b", amount: 2 },
  }));
  // entry 10/30 は 02-15、未紐付け 2 は 02-16 → [2(02-16), 3(02-15,id3), 1(02-15,id1)]
  assert.deepEqual(cards.map((c) => c.voucher_id), [2, 3, 1]);
});

test("filter: 日付範囲は effective_date で絞り込む", () => {
  const cards = buildVoucherCards([
    meta({ id: 1, journal_entry_id: 10, fiscal_year: 2026, uploaded_at: "2026-01-10T00:00:00" }),
    meta({ id: 2, journal_entry_id: 20, fiscal_year: 2026, uploaded_at: "2026-02-20T00:00:00" }),
  ], entryMap({
    10: { date: "2026-01-10", description: "a", amount: 100 },
    20: { date: "2026-02-20", description: "b", amount: 200 },
  }));
  const r = filterVoucherCards(cards, { dateFrom: "2026-02-01" });
  assert.deepEqual(r.map((c) => c.voucher_id), [2]);
});

test("filter: 金額範囲。amount が無いカードは金額条件指定時に除外", () => {
  const cards = buildVoucherCards([
    meta({ id: 1, journal_entry_id: 10, fiscal_year: 2026, uploaded_at: "2026-01-10T00:00:00" }),
    meta({ id: 2, journal_entry_id: null, uploaded_at: "2026-02-20T00:00:00" }), // amount null
  ], entryMap({ 10: { date: "2026-01-10", description: "a", amount: 5000 } }));
  const r = filterVoucherCards(cards, { amountFrom: "1000" });
  assert.deepEqual(r.map((c) => c.voucher_id), [1]);
});

test("filter: 摘要部分一致。description 空のカードは検索指定時に除外", () => {
  const cards = buildVoucherCards([
    meta({ id: 1, journal_entry_id: 10, fiscal_year: 2026, uploaded_at: "2026-01-10T00:00:00" }),
    meta({ id: 2, journal_entry_id: 20, fiscal_year: 2026, uploaded_at: "2026-01-11T00:00:00" }),
    meta({ id: 3, journal_entry_id: null, uploaded_at: "2026-01-12T00:00:00" }),
  ], entryMap({
    10: { date: "2026-01-10", description: "コンビニ購入", amount: 100 },
    20: { date: "2026-01-11", description: "ランチ代", amount: 200 },
  }));
  const r = filterVoucherCards(cards, { search: "コンビニ" });
  assert.deepEqual(r.map((c) => c.voucher_id), [1]);
});

test("filter: 条件なしは全件", () => {
  const cards = buildVoucherCards([
    meta({ id: 1, journal_entry_id: null, uploaded_at: "2026-01-10T00:00:00" }),
    meta({ id: 2, journal_entry_id: null, uploaded_at: "2026-01-11T00:00:00" }),
  ], new Map());
  assert.equal(filterVoucherCards(cards, {}).length, 2);
});

test("空 meta は [] を返す", () => {
  assert.deepEqual(buildVoucherCards([], new Map()), []);
  assert.deepEqual(buildVoucherCards(null, null), []);
});

test("整数でない id の証憑は除外する (XSS 経路の遮断・防御的検証)", () => {
  const cards = buildVoucherCards([
    meta({ id: "1; alert(1)", journal_entry_id: null, uploaded_at: "2026-01-10T00:00:00" }),
    meta({ id: 5, journal_entry_id: null, uploaded_at: "2026-01-11T00:00:00" }),
  ], new Map());
  assert.deepEqual(cards.map((c) => c.voucher_id), [5]);
});
