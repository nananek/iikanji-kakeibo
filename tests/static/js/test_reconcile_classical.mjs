// crypto/reconcile/classical.findMatches の Node 単体テスト。
//
// サーバ tests/test_reconciliation.py (削除済) と同等のケースを移植し、
// 決定論的マッチング / journal_only / daily_summary の振る舞いを検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const MOD = new URL(
  "../../../app/static/js/crypto/reconcile/classical.js",
  import.meta.url,
);
const {
  findMatches, classifyBand,
  MATCH_DATE_TOLERANCE, MATCH_DATE_BAND_EXACT,
  MATCH_DATE_BAND_WARN, MATCH_DATE_BAND_CAUTION,
} = await import(MOD.href);


const PAYMENT = "1020";
const NAMES = { "5010": "食費", "1020": "普通預金", "1010": "現金" };

let _seq = 0;
function entry({
  date, debitCode, creditCode, amount,
  description = "", source = "cashbook", id,
}) {
  return {
    id: id ?? ++_seq,
    date,
    description,
    source,
    lines: [
      { account_code: debitCode, debit: amount, credit: 0 },
      { account_code: creditCode, debit: 0, credit: amount },
    ],
  };
}

// 通常の利用 (出金): 費目 5010 / 支払元 1020 → 支払元行は credit (withdrawal)。
function expenseEntry(date, amount, opts = {}) {
  return entry({ date, debitCode: "5010", creditCode: PAYMENT, amount, ...opts });
}
// 返金/入金: 支払元 1020 / 費目 5010 → 支払元行は debit (deposit)。
function refundEntry(date, amount, opts = {}) {
  return entry({ date, debitCode: PAYMENT, creditCode: "5010", amount, ...opts });
}

function csvRow(date, amount, opts = {}) {
  const direction = opts.direction || "withdrawal";
  return {
    date,
    description: opts.description || "購入",
    deposit: direction === "deposit" ? amount : 0,
    withdrawal: direction === "withdrawal" ? amount : 0,
  };
}

function run(csvRows, journalEntries, opts = {}) {
  return findMatches({
    paymentAccountCode: PAYMENT,
    csvRows,
    journalEntries,
    accountName: opts.accountName ?? NAMES,
    today: opts.today,
  });
}


// ============ classifyBand / 定数 ============

test("classifyBand: 境界値", () => {
  assert.equal(classifyBand(0), "exact");
  assert.equal(classifyBand(1), "warn");
  assert.equal(classifyBand(3), "warn");
  assert.equal(classifyBand(4), "caution");
  assert.equal(classifyBand(7), "caution");
});

test("定数が Python 版と一致", () => {
  assert.equal(MATCH_DATE_TOLERANCE, 7);
  assert.equal(MATCH_DATE_BAND_EXACT, 0);
  assert.equal(MATCH_DATE_BAND_WARN, 3);
  assert.equal(MATCH_DATE_BAND_CAUTION, 7);
});


// ============ 基本マッチング ============

test("exact match: 金額・日付完全一致 → matched", () => {
  const res = run(
    [csvRow("2026-01-10", 1500, { description: "コンビニ" })],
    [expenseEntry("2026-01-10", 1500, { source: "ai_receipt" })],
  );
  assert.equal(res.csv_results.length, 1);
  const r = res.csv_results[0];
  assert.equal(r.status, "matched");
  assert.equal(r.csv_index, 0);
  assert.equal(r.matches.length, 1);
  const m = r.matches[0];
  assert.equal(m.amount, 1500);
  assert.equal(m.date, "2026-01-10");
  assert.equal(m.source, "ai_receipt");
  assert.equal(m.date_band, "exact");
  assert.equal(m.date_diff_days, 0);
  assert.ok(m.category_name.includes("食費"));
  // _line_id は出力から除去される
  assert.equal(m._line_id, undefined);
});

test("日付 1 日差 → matched (warn, diff -1)", () => {
  const res = run(
    [csvRow("2026-01-10", 2000)],
    [expenseEntry("2026-01-11", 2000)],
  );
  assert.equal(res.csv_results[0].status, "matched");
  assert.equal(res.csv_results[0].matches[0].date_band, "warn");
  assert.equal(res.csv_results[0].matches[0].date_diff_days, -1);
});

test("8 日差 (トレランス外) → unmatched", () => {
  const res = run(
    [csvRow("2026-01-10", 2000)],
    [expenseEntry("2026-01-18", 2000)],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("ちょうど 7 日差 → matched (caution)", () => {
  // CSV 1/17 vs 仕訳 1/10 → diff = CSV - 仕訳 = +7
  const res = run(
    [csvRow("2026-01-17", 1000)],
    [expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "matched");
  assert.equal(res.csv_results[0].matches[0].date_diff_days, 7);
  assert.equal(res.csv_results[0].matches[0].date_band, "caution");
});

test("+5 日差 → caution", () => {
  const res = run(
    [csvRow("2026-01-15", 2000)],
    [expenseEntry("2026-01-10", 2000)],
  );
  assert.equal(res.csv_results[0].matches[0].date_diff_days, 5);
  assert.equal(res.csv_results[0].matches[0].date_band, "caution");
});

test("同日同金額 2 件 → multiple", () => {
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [expenseEntry("2026-01-10", 1000), expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "multiple");
  assert.equal(res.csv_results[0].matches.length, 2);
});

test("金額不一致 → unmatched", () => {
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [expenseEntry("2026-01-10", 999)],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("deposit 方向の一致", () => {
  const res = run(
    [csvRow("2026-01-10", 500, { direction: "deposit", description: "返金" })],
    [refundEntry("2026-01-10", 500, { source: "journal" })],
  );
  assert.equal(res.csv_results[0].status, "matched");
  assert.equal(res.csv_results[0].matches[0].amount, 500);
});

test("CSV 行に日付なし → unmatched", () => {
  const res = run(
    [{ date: null, description: "日付なし", withdrawal: 1000, deposit: 0 }],
    [expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("不正な日付文字列 → unmatched", () => {
  const res = run(
    [csvRow("invalid-date", 1000)],
    [expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("ありえない日 (2/30) → unmatched", () => {
  const res = run(
    [csvRow("2026-02-30", 1000)],
    [expenseEntry("2026-02-28", 1000)],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("入出金ゼロ → unmatched", () => {
  const res = run(
    [{ date: "2026-01-10", description: "ゼロ", withdrawal: 0, deposit: 0 }],
    [],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("空 CSV → 全て空", () => {
  const res = run([], [expenseEntry("2026-01-10", 1000)]);
  assert.deepEqual(res.csv_results, []);
  assert.deepEqual(res.journal_only, []);
  assert.deepEqual(res.daily_summary, []);
});

test("全 CSV 行が不正日付 → 全 unmatched・サマリー空", () => {
  const res = run(
    [csvRow("invalid", 1000), csvRow("bad", 500)],
    [expenseEntry("2026-01-10", 1000)],
  );
  for (const r of res.csv_results) assert.equal(r.status, "unmatched");
  assert.deepEqual(res.daily_summary, []);
  assert.deepEqual(res.journal_only, []);
});

test("支払元口座以外の行はマッチ候補にならない", () => {
  // 1010 (現金) / 5010 の仕訳 — 支払元 1020 を含まない
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [entry({ date: "2026-01-10", debitCode: "5010", creditCode: "1010", amount: 1000 })],
  );
  assert.equal(res.csv_results[0].status, "unmatched");
});

test("date が不正な entry はスキップされる", () => {
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [expenseEntry("bad-date", 1000), expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "matched");
});


// ============ 重複防止・貪欲法 ============

test("1 仕訳が複数 CSV 行に重複マッチしない", () => {
  const res = run(
    [csvRow("2026-01-10", 800, { description: "1行目" }),
     csvRow("2026-01-10", 800, { description: "2行目" })],
    [expenseEntry("2026-01-10", 800)],
  );
  assert.equal(res.csv_results[0].status, "matched");
  assert.equal(res.csv_results[1].status, "unmatched");
});

test("exact を warn より優先確定 (近い CSV 行が勝つ)", () => {
  const res = run(
    [csvRow("2026-01-10", 1000, { description: "CSV-10" }),
     csvRow("2026-01-12", 1000, { description: "CSV-12" })],
    [expenseEntry("2026-01-12", 1000)],
  );
  const statuses = res.csv_results.map((r) => r.status);
  assert.deepEqual(statuses, ["unmatched", "matched"]);
  assert.equal(res.csv_results[1].matches[0].date_band, "exact");
});

test("両方 warn なら近い候補を先頭に (multiple)", () => {
  const res = run(
    [csvRow("2026-01-10", 2500)],
    [expenseEntry("2026-01-11", 2500, { description: "近い仕訳" }),
     expenseEntry("2026-01-13", 2500, { description: "遠い仕訳" })],
  );
  const r = res.csv_results[0];
  assert.equal(r.status, "multiple");
  assert.equal(r.matches[0].date, "2026-01-11");
  assert.equal(Math.abs(r.matches[0].date_diff_days), 1);
});

test("同一仕訳の同一口座複数行が個別に CSV 行とマッチ", () => {
  const multiLine = {
    id: 9001,
    date: "2026-01-10",
    description: "ゲーム課金",
    source: "cashbook",
    lines: [
      { account_code: "5010", debit: 24000, credit: 0 },
      { account_code: PAYMENT, debit: 0, credit: 12000 },
      { account_code: PAYMENT, debit: 0, credit: 12000 },
    ],
  };
  const res = run(
    [csvRow("2026-01-10", 12000, { description: "課金1" }),
     csvRow("2026-01-10", 12000, { description: "課金2" })],
    [multiLine],
  );
  const statuses = res.csv_results.map((r) => r.status);
  const matchedCount =
    statuses.filter((s) => s === "matched").length +
    statuses.filter((s) => s === "multiple").length;
  assert.equal(matchedCount, 2);
});


// ============ journal_only ============

test("CSV にマッチしない仕訳が journal_only に", () => {
  const res = run(
    [csvRow("2026-01-10", 999, { description: "別取引" })],
    [expenseEntry("2026-01-10", 1500, { description: "CSVにない仕訳" })],
  );
  assert.equal(res.journal_only.length, 1);
  assert.equal(res.journal_only[0].amount, 1500);
  assert.equal(res.journal_only[0].description, "CSVにない仕訳");
});

test("マッチした仕訳は journal_only に含まれない", () => {
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "matched");
  assert.equal(res.journal_only.length, 0);
});

test("multiple 候補の仕訳も journal_only に含まれない", () => {
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [expenseEntry("2026-01-10", 1000), expenseEntry("2026-01-10", 1000)],
  );
  assert.equal(res.csv_results[0].status, "multiple");
  assert.equal(res.journal_only.length, 0);
});

test("journal_only に days_since_journal / is_stale", () => {
  const res = run(
    [csvRow("2026-01-10", 9999)],
    [expenseEntry("2026-01-10", 1500, { source: "ai_receipt" })],
    { today: new Date(Date.UTC(2026, 1, 11)) }, // 2026-02-11 → 32 日
  );
  const j = res.journal_only[0];
  assert.equal(j.days_since_journal, 32);
  assert.equal(j.is_stale, true);
});

test("journal_only: 30 日以内は is_stale=false", () => {
  const res = run(
    [csvRow("2026-01-10", 9999)],
    [expenseEntry("2026-01-10", 1500)],
    { today: new Date(Date.UTC(2026, 0, 25)) }, // 15 日
  );
  const j = res.journal_only[0];
  assert.equal(j.days_since_journal, 15);
  assert.equal(j.is_stale, false);
});

test("journal_only は経過日数の降順", () => {
  const res = run(
    [csvRow("2026-01-05", 9999), csvRow("2026-01-10", 9998)],
    [expenseEntry("2026-01-05", 100, { description: "古い" }),
     expenseEntry("2026-01-10", 200, { description: "新しい" }),
     expenseEntry("2026-01-08", 300, { description: "中間" })],
    { today: new Date(Date.UTC(2026, 1, 1)) },
  );
  assert.equal(res.journal_only.length, 3);
  assert.deepEqual(
    res.journal_only.map((j) => j.description),
    ["古い", "中間", "新しい"],
  );
});

test("journal_only は CSV 範囲外 (過去) を除外", () => {
  const res = run(
    [csvRow("2026-04-16", 9999), csvRow("2026-05-15", 8888)],
    [expenseEntry("2026-04-10", 500, { description: "範囲外" })],
  );
  assert.ok(res.journal_only.every((j) => j.date >= "2026-04-16"));
});

test("journal_only は CSV 範囲外 (未来) を除外", () => {
  const res = run(
    [csvRow("2026-04-16", 9999), csvRow("2026-05-15", 8888)],
    [expenseEntry("2026-05-20", 600, { description: "範囲外未来" })],
  );
  assert.ok(res.journal_only.every((j) => j.date <= "2026-05-15"));
});

test("journal_only は境界日を含む", () => {
  const res = run(
    [csvRow("2026-04-16", 9999), csvRow("2026-05-15", 8888)],
    [expenseEntry("2026-04-16", 100, { description: "min境界" }),
     expenseEntry("2026-05-15", 200, { description: "max境界" })],
  );
  assert.deepEqual(
    res.journal_only.map((j) => j.description).sort(),
    ["max境界", "min境界"],
  );
});

test("範囲制限は journal_only にだけ効きマッチングは ±7 日", () => {
  const res = run(
    [csvRow("2026-04-16", 1000)],
    [expenseEntry("2026-04-14", 1000)],
  );
  assert.equal(res.csv_results[0].status, "matched");
  assert.equal(res.csv_results[0].matches[0].date_band, "warn");
  assert.deepEqual(res.journal_only, []);
});


// ============ journal_only 方向フィルタ ============

test("出金のみ CSV のとき引き落とし仕訳 (deposit) を除外", () => {
  const res = run(
    [csvRow("2026-04-16", 9999), csvRow("2026-04-27", 8888)],
    [expenseEntry("2026-04-16", 1500, { description: "コンビニ" }),
     // 引き落とし: 支払元 debit (deposit 方向)
     entry({ date: "2026-04-27", debitCode: PAYMENT, creditCode: "1010",
             amount: 50000, description: "引き落とし" })],
  );
  const descs = res.journal_only.map((j) => j.description);
  assert.ok(descs.includes("コンビニ"));
  assert.ok(!descs.includes("引き落とし"));
});

test("入金のみ CSV のとき通常購入 (withdrawal) を除外", () => {
  const res = run(
    [csvRow("2026-04-16", 9999, { direction: "deposit" }),
     csvRow("2026-04-20", 8888, { direction: "deposit" })],
    [refundEntry("2026-04-16", 500, { description: "返金" }),
     expenseEntry("2026-04-20", 1500, { description: "通常購入" })],
  );
  const descs = res.journal_only.map((j) => j.description);
  assert.ok(descs.includes("返金"));
  assert.ok(!descs.includes("通常購入"));
});

test("CSV に両方向あれば両方向の仕訳を対象", () => {
  const res = run(
    [csvRow("2026-04-16", 9999),
     csvRow("2026-04-20", 8888, { direction: "deposit" })],
    [expenseEntry("2026-04-16", 1500, { description: "出金仕訳" }),
     refundEntry("2026-04-20", 500, { description: "入金仕訳" })],
  );
  const descs = res.journal_only.map((j) => j.description);
  assert.ok(descs.includes("出金仕訳"));
  assert.ok(descs.includes("入金仕訳"));
});


// ============ daily_summary ============

test("一致日は差異なし", () => {
  const res = run(
    [csvRow("2026-01-10", 1000)],
    [expenseEntry("2026-01-10", 1000)],
  );
  const day = res.daily_summary.find((s) => s.date === "2026-01-10");
  assert.equal(day.csv_count, 1);
  assert.equal(day.journal_count, 1);
  assert.equal(day.diff_amount, 0);
  assert.equal(day.has_discrepancy, false);
});

test("CSV のみの日 → 差異あり", () => {
  const res = run([csvRow("2026-01-10", 500)], []);
  assert.equal(res.daily_summary.length, 1);
  assert.equal(res.daily_summary[0].csv_count, 1);
  assert.equal(res.daily_summary[0].journal_count, 0);
  assert.equal(res.daily_summary[0].has_discrepancy, true);
});

test("件数差を検出 (CSV3 / 仕訳2)", () => {
  const res = run(
    [csvRow("2026-01-10", 500), csvRow("2026-01-10", 500), csvRow("2026-01-10", 500)],
    [expenseEntry("2026-01-10", 500), expenseEntry("2026-01-10", 500)],
  );
  const day = res.daily_summary.find((s) => s.date === "2026-01-10");
  assert.equal(day.csv_count, 3);
  assert.equal(day.journal_count, 2);
  assert.equal(day.diff_count, 1);
  assert.equal(day.diff_amount, 500);
  assert.equal(day.has_discrepancy, true);
});

test("仕訳が多い件数差 (CSV2 / 仕訳3)", () => {
  const res = run(
    [csvRow("2026-01-10", 500), csvRow("2026-01-10", 500)],
    [expenseEntry("2026-01-10", 500), expenseEntry("2026-01-10", 500),
     expenseEntry("2026-01-10", 500)],
  );
  const day = res.daily_summary.find((s) => s.date === "2026-01-10");
  assert.equal(day.diff_count, -1);
  assert.equal(day.diff_amount, -500);
});

test("日跨ぎマッチは各側の本来日付に集計 + cross_day_matched", () => {
  const res = run(
    [csvRow("2026-01-10", 1500)],
    [expenseEntry("2026-01-12", 1500, { source: "ai_receipt" })],
  );
  const map = Object.fromEntries(res.daily_summary.map((s) => [s.date, s]));
  assert.equal(map["2026-01-10"].csv_count, 1);
  assert.equal(map["2026-01-10"].journal_count, 0);
  assert.equal(map["2026-01-12"].csv_count, 0);
  assert.equal(map["2026-01-12"].journal_count, 1);
  assert.equal(map["2026-01-10"].cross_day_matched, 1);
  assert.equal(map["2026-01-12"].cross_day_matched, 0);
});

test("exact マッチは cross_day_matched=0", () => {
  const res = run(
    [csvRow("2026-01-10", 1500)],
    [expenseEntry("2026-01-10", 1500)],
  );
  const day = res.daily_summary.find((s) => s.date === "2026-01-10");
  assert.equal(day.cross_day_matched, 0);
});

test("pending_card_amount にその日の未達合計", () => {
  const res = run(
    [csvRow("2026-01-10", 9999)],
    [expenseEntry("2026-01-10", 1500)],
  );
  const day = res.daily_summary.find((s) => s.date === "2026-01-10");
  assert.equal(day.pending_card_amount, 1500);
});

test("全マッチなら pending_card_amount=0", () => {
  const res = run(
    [csvRow("2026-01-10", 1500)],
    [expenseEntry("2026-01-10", 1500)],
  );
  const day = res.daily_summary.find((s) => s.date === "2026-01-10");
  assert.equal(day.pending_card_amount, 0);
});

test("不正日付の CSV 行はサマリーから除外", () => {
  const res = run(
    [csvRow("2026-01-10", 1000),
     { date: null, description: "なし", withdrawal: 500, deposit: 0 },
     csvRow("invalid", 300)],
    [expenseEntry("2026-01-10", 1000)],
  );
  const day = res.daily_summary.filter((s) => s.date === "2026-01-10");
  assert.equal(day.length, 1);
  assert.equal(day[0].csv_count, 1);
});


// ============ accountName 解決のバリエーション ============

test("accountName を関数で渡せる", () => {
  const res = run(
    [csvRow("2026-01-10", 1500)],
    [expenseEntry("2026-01-10", 1500)],
    { accountName: (code) => (code === "5010" ? "食費" : "") },
  );
  assert.ok(res.csv_results[0].matches[0].category_name.includes("食費"));
});

test("accountName 未指定なら category_name は空", () => {
  // run() ヘルパは undefined を NAMES にフォールバックするので直接呼ぶ。
  const res = findMatches({
    paymentAccountCode: PAYMENT,
    csvRows: [csvRow("2026-01-10", 1500)],
    journalEntries: [expenseEntry("2026-01-10", 1500)],
  });
  assert.equal(res.csv_results[0].matches[0].category_name, "");
});
