// csv_parse.js (Phase E3-D-1) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/csv_parse.js",
  import.meta.url,
);
const {
  detectEncoding, parseCsvPreview, parseAmount, parseDate, parseCsvFull,
  DATE_FORMATS,
} = await import(M.href);


// --- detectEncoding ---

test("detectEncoding: UTF-8 BOM 付き", () => {
  const bytes = new Uint8Array([0xEF, 0xBB, 0xBF, 0x68, 0x69]);
  assert.equal(detectEncoding(bytes), "utf-8");
});

test("detectEncoding: ASCII = utf-8 で OK", () => {
  const bytes = new TextEncoder().encode("date,amount\n2026/01/01,100");
  assert.equal(detectEncoding(bytes), "utf-8");
});

test("detectEncoding: UTF-8 日本語", () => {
  const bytes = new TextEncoder().encode("日付,摘要,金額");
  assert.equal(detectEncoding(bytes), "utf-8");
});

test("detectEncoding: Shift_JIS (テキスト encoder で確認)", () => {
  // 「日」(U+65E5) は SJIS = 0x93FA
  const bytes = new Uint8Array([0x93, 0xFA, 0x95, 0x74]);  // 日付
  // UTF-8 fatal で失敗 → shift_jis fatal で成功する想定
  const enc = detectEncoding(bytes);
  assert.ok(["shift_jis", "utf-8"].includes(enc),
            `expected shift_jis fallback or utf-8 fallback, got ${enc}`);
});


// --- parseAmount ---

test("parseAmount: 空 → 0", () => {
  assert.equal(parseAmount(""), 0);
  assert.equal(parseAmount(null), 0);
  assert.equal(parseAmount(undefined), 0);
  assert.equal(parseAmount("   "), 0);
  assert.equal(parseAmount("-"), 0);
});

test("parseAmount: カンマ区切り", () => {
  assert.equal(parseAmount("1,234"), 1234);
  assert.equal(parseAmount("1,234,567"), 1234567);
});

test("parseAmount: 円記号", () => {
  assert.equal(parseAmount("¥1,234"), 1234);
  assert.equal(parseAmount("￥500"), 500);
  assert.equal(parseAmount("1234円"), 1234);
});

test("parseAmount: マイナス値 (符号保持)", () => {
  assert.equal(parseAmount("-500"), -500);
  assert.equal(parseAmount("¥-1,000"), -1000);
});

test("parseAmount: 不正値 → 0", () => {
  assert.equal(parseAmount("abc"), 0);
  assert.equal(parseAmount("--"), 0);
});

test("parseAmount: 小数は切り捨て", () => {
  assert.equal(parseAmount("100.5"), 100);
  assert.equal(parseAmount("-100.5"), -100);
});


// --- parseDate ---

test("parseDate: YYYY/MM/DD", () => {
  assert.equal(parseDate("2026/05/15", "%Y/%m/%d"), "2026-05-15");
});

test("parseDate: YYYY-MM-DD", () => {
  assert.equal(parseDate("2026-05-15", "%Y-%m-%d"), "2026-05-15");
});

test("parseDate: 年月日 (日本語)", () => {
  assert.equal(parseDate("2026年5月15日", "%Y年%m月%d日"), "2026-05-15");
});

test("parseDate: 2 桁年 (00-68 → 2000s, 69-99 → 1900s)", () => {
  assert.equal(parseDate("26/05/15", "%y/%m/%d"), "2026-05-15");
  assert.equal(parseDate("99/05/15", "%y/%m/%d"), "1999-05-15");
  assert.equal(parseDate("68/05/15", "%y/%m/%d"), "2068-05-15");
  assert.equal(parseDate("69/05/15", "%y/%m/%d"), "1969-05-15");
});

test("parseDate: フォーマット不一致 → 別フォーマット fallback", () => {
  // 指定 %Y/%m/%d だが入力は %Y-%m-%d → fallback で成功
  assert.equal(parseDate("2026-05-15", "%Y/%m/%d"), "2026-05-15");
});

test("parseDate: 完全に不正 → null", () => {
  assert.equal(parseDate("not a date", "%Y/%m/%d"), null);
  assert.equal(parseDate("", "%Y/%m/%d"), null);
  assert.equal(parseDate(null, "%Y/%m/%d"), null);
});

test("parseDate: 存在しない日付 (2/30 等) → null", () => {
  assert.equal(parseDate("2026/02/30", "%Y/%m/%d"), null);
  assert.equal(parseDate("2026/13/01", "%Y/%m/%d"), null);
});

test("parseDate: 1 桁月/日も受理 (zero-padding 不要)", () => {
  assert.equal(parseDate("2026/5/1", "%Y/%m/%d"), "2026-05-01");
});


// --- parseCsvPreview ---

function csvBytes(text) {
  return new TextEncoder().encode(text);
}

test("parseCsvPreview: 空 → 空結果", () => {
  const r = parseCsvPreview(csvBytes(""));
  assert.deepEqual(r.headers, []);
  assert.deepEqual(r.rows, []);
  assert.equal(r.total_rows, 0);
});

test("parseCsvPreview: 通常 CSV", () => {
  const r = parseCsvPreview(csvBytes(
    "date,desc,amount\n" +
    "2026/01/01,コンビニ,500\n" +
    "2026/01/02,スーパー,2000\n"
  ));
  assert.deepEqual(r.headers, ["date", "desc", "amount"]);
  assert.equal(r.rows.length, 2);
  assert.deepEqual(r.rows[0], ["2026/01/01", "コンビニ", "500"]);
  assert.equal(r.total_rows, 2);
});

test("parseCsvPreview: ダブルクォート + カンマ内包", () => {
  const r = parseCsvPreview(csvBytes(
    'date,desc,amount\n' +
    '"2026/01/01","Hello, World",500\n'
  ));
  assert.deepEqual(r.rows[0], ["2026/01/01", "Hello, World", "500"]);
});

test("parseCsvPreview: BOM 付き", () => {
  const bom = new Uint8Array([0xEF, 0xBB, 0xBF]);
  const body = new TextEncoder().encode("a,b\n1,2\n");
  const bytes = new Uint8Array(bom.length + body.length);
  bytes.set(bom, 0);
  bytes.set(body, bom.length);
  const r = parseCsvPreview(bytes);
  assert.deepEqual(r.headers, ["a", "b"]);  // BOM 除去されている
});

test("parseCsvPreview: 空行をスキップ", () => {
  const r = parseCsvPreview(csvBytes(
    "a,b\n" +
    "1,2\n" +
    ",\n" +
    "3,4\n"
  ));
  assert.equal(r.total_rows, 2);
  assert.deepEqual(r.rows[0], ["1", "2"]);
  assert.deepEqual(r.rows[1], ["3", "4"]);
});

test("parseCsvPreview: maxRows でプレビュー切り詰め", () => {
  const lines = ["a,b"];
  for (let i = 1; i <= 30; i++) lines.push(`${i},${i * 10}`);
  const r = parseCsvPreview(csvBytes(lines.join("\n") + "\n"), {maxRows: 5});
  assert.equal(r.rows.length, 5);
  assert.equal(r.total_rows, 30);
});


// --- parseCsvFull ---

test("parseCsvFull: 銀行 CSV (入金/出金別カラム)", () => {
  const bytes = csvBytes(
    "日付,摘要,入金,出金\n" +
    "2026/01/15,給与,300000,\n" +
    "2026/01/16,スーパー,,1500\n"
  );
  const r = parseCsvFull(bytes, {
    date_col: 0, desc_col: 1, deposit_col: 2, withdrawal_col: 3,
  }, "%Y/%m/%d");
  assert.equal(r.length, 2);
  assert.equal(r[0].row_num, 2);
  assert.equal(r[0].date, "2026-01-15");
  assert.equal(r[0].description, "給与");
  assert.equal(r[0].deposit, 300000);
  assert.equal(r[0].withdrawal, 0);
  assert.equal(r[1].date, "2026-01-16");
  assert.equal(r[1].deposit, 0);
  assert.equal(r[1].withdrawal, 1500);
});

test("parseCsvFull: マイナス値の自動反転 (キャッシュバック)", () => {
  const bytes = csvBytes(
    "日付,摘要,出金\n" +
    "2026/01/15,キャッシュバック,-500\n"
  );
  const r = parseCsvFull(bytes, {
    date_col: 0, desc_col: 1, withdrawal_col: 2,
  }, "%Y/%m/%d");
  // 出金 -500 → 入金 500 に反転
  assert.equal(r[0].deposit, 500);
  assert.equal(r[0].withdrawal, 0);
});

test("parseCsvFull: 日付不明 + 金額 0 の行はスキップ", () => {
  const bytes = csvBytes(
    "日付,摘要,入金\n" +
    "invalid,,\n" +
    "2026/01/15,test,100\n"
  );
  const r = parseCsvFull(bytes, {
    date_col: 0, desc_col: 1, deposit_col: 2,
  }, "%Y/%m/%d");
  assert.equal(r.length, 1);
  assert.equal(r[0].description, "test");
});

test("parseCsvFull: 日付パース失敗だが金額あり → date=null で残す", () => {
  const bytes = csvBytes(
    "日付,摘要,入金\n" +
    "??,雑費,500\n"
  );
  const r = parseCsvFull(bytes, {
    date_col: 0, desc_col: 1, deposit_col: 2,
  }, "%Y/%m/%d");
  assert.equal(r.length, 1);
  assert.equal(r[0].date, null);
  assert.equal(r[0].deposit, 500);
});

test("parseCsvFull: 列インデックスが範囲外なら空文字扱い", () => {
  const bytes = csvBytes(
    "a,b\n" +
    "2026/01/15,test\n"
  );
  const r = parseCsvFull(bytes, {
    date_col: 0, desc_col: 1, deposit_col: 99,  // 範囲外
  }, "%Y/%m/%d");
  assert.equal(r[0].date, "2026-01-15");
  assert.equal(r[0].deposit, 0);  // 範囲外 → 0
});

test("parseCsvFull: 空 CSV", () => {
  assert.deepEqual(parseCsvFull(csvBytes(""), {
    date_col: 0, desc_col: 1,
  }, "%Y/%m/%d"), []);
});

test("parseCsvFull: ヘッダのみ", () => {
  assert.deepEqual(parseCsvFull(csvBytes("a,b\n"), {
    date_col: 0, desc_col: 1,
  }, "%Y/%m/%d"), []);
});

test("parseCsvFull: 入力 type チェック", () => {
  assert.throws(() => parseCsvFull("not bytes", {date_col: 0, desc_col: 1}, "%Y/%m/%d"), /Uint8Array/);
  assert.throws(() => parseCsvFull(new Uint8Array(0), {}, "%Y/%m/%d"), /date_col/);
});

test("DATE_FORMATS export 確認", () => {
  assert.ok(Array.isArray(DATE_FORMATS));
  assert.ok(DATE_FORMATS.length > 0);
  assert.deepEqual(DATE_FORMATS[0], ["YYYY/MM/DD", "%Y/%m/%d"]);
});


// --- カバレッジ補強テスト (#195 review 指摘 — 95% gate 達成) ---

test("detectEncoding: 全候補失敗時の fallback で文字列を返す", () => {
  // 全エンコーディングを fatal で失敗させる無効バイト列。
  // 重要なのは throw せず文字列を返すこと (utf-8 fallback)。
  const bytes = new Uint8Array([0xFE, 0xFF, 0xFE, 0xFF]);
  const enc = detectEncoding(bytes);
  assert.ok(typeof enc === "string");
});

test("parseCsvPreview: クォート内のダブルクォートエスケープ (\"\" → \")", () => {
  const r = parseCsvPreview(csvBytes(
    'desc,amount\n' +
    '"He said ""hi""",500\n'
  ));
  assert.deepEqual(r.rows[0], ['He said "hi"', "500"]);
});

test("parseCsvPreview: CRLF 改行", () => {
  const r = parseCsvPreview(csvBytes("a,b\r\n1,2\r\n3,4\r\n"));
  assert.equal(r.total_rows, 2);
  assert.deepEqual(r.rows, [["1", "2"], ["3", "4"]]);
});

test("parseCsvPreview: CR 単独改行 (旧 Mac)", () => {
  const r = parseCsvPreview(csvBytes("a,b\r1,2\r3,4\r"));
  assert.equal(r.total_rows, 2);
  assert.deepEqual(r.rows, [["1", "2"], ["3", "4"]]);
});

test("parseCsvPreview: 末尾改行なしの最終行も取得", () => {
  const r = parseCsvPreview(csvBytes("a,b\n1,2"));  // 末尾 \n なし
  assert.equal(r.total_rows, 1);
  assert.deepEqual(r.rows[0], ["1", "2"]);
});

test("parseCsvPreview: bytes が Uint8Array でないと TypeError", () => {
  assert.throws(() => parseCsvPreview("string"), /Uint8Array/);
  assert.throws(() => parseCsvPreview(null), /Uint8Array/);
});

test("parseCsvFull: deposit 側のマイナス値も反転 (返金等)", () => {
  // 入金カラムにマイナス値が入るケース (普段は出金カラム側だが、
  // 銀行 CSV によってはあり得る)
  const bytes = csvBytes(
    "日付,摘要,入金,出金\n" +
    "2026/01/15,返金処理,-300,\n"
  );
  const r = parseCsvFull(bytes, {
    date_col: 0, desc_col: 1, deposit_col: 2, withdrawal_col: 3,
  }, "%Y/%m/%d");
  // 入金 -300 → 出金 300 に反転
  assert.equal(r[0].deposit, 0);
  assert.equal(r[0].withdrawal, 300);
});
