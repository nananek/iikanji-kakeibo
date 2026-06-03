// E6 (#113) 全データエクスポート: export/csv.js の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const {
  escapeCell, toCsv, buildAccountNameMap,
  buildJournalCsv, buildAccountsCsv, buildMedicalCsv, buildVouchersCsv,
} = await import(
  new URL("../../../app/static/js/export/csv.js", import.meta.url).href
);


test("escapeCell: 通常値はそのまま", () => {
  assert.equal(escapeCell("abc"), "abc");
  assert.equal(escapeCell(123), "123");
});

test("escapeCell: null/undefined は空文字", () => {
  assert.equal(escapeCell(null), "");
  assert.equal(escapeCell(undefined), "");
});

test("escapeCell: カンマ・改行・引用符は quote", () => {
  assert.equal(escapeCell("a,b"), '"a,b"');
  assert.equal(escapeCell("a\nb"), '"a\nb"');
  assert.equal(escapeCell('a"b'), '"a""b"');
  assert.equal(escapeCell("a\r\nb"), '"a\r\nb"');
});

test("escapeCell: 日本語はそのまま (quote 不要)", () => {
  assert.equal(escapeCell("摘要テスト"), "摘要テスト");
});

test("toCsv: ヘッダ + 行を CRLF 区切りで出力", () => {
  const out = toCsv(["A", "B"], [[1, 2], [3, 4]]);
  assert.equal(out, "A,B\r\n1,2\r\n3,4\r\n");
});

test("toCsv: 空行リストはヘッダのみ", () => {
  assert.equal(toCsv(["A", "B"], []), "A,B\r\n");
});

test("buildAccountNameMap: code -> name", () => {
  const m = buildAccountNameMap([
    { code: "1010", name: "現金" },
    { code: "5010", name: "消耗品費" },
  ]);
  assert.equal(m.get("1010"), "現金");
  assert.equal(m.get("5010"), "消耗品費");
  assert.equal(m.get("9999"), undefined);
});


const SAMPLE = {
  accounts: [
    { code: "1010", name: "現金", description: "", tax_category: "",
      cost_type: "", system_role: null, is_active: true, deactivated_year: null },
    { code: "5010", name: "消耗品費", description: "備品", tax_category: "課税",
      cost_type: "expense", system_role: null, is_active: false,
      deactivated_year: 2024 },
  ],
  journal_entries: [
    { id: 1, entry_number: 1, fiscal_year: 2026, fiscal_month: 3,
      date: "2026-03-01", description: "ノート購入", source: "journal" },
    { id: 2, entry_number: 2, fiscal_year: 2026, fiscal_month: 3,
      _decryptError: "bad tag" },
  ],
  journal_entry_lines: [
    { id: 10, journal_entry_id: 1, account_code: "5010",
      debit_amount: 500, credit_amount: 0 },
    { id: 11, journal_entry_id: 1, account_code: "1010",
      debit_amount: 0, credit_amount: 500 },
    { id: 12, journal_entry_id: 2, account_code: "9999",
      debit_amount: 100, credit_amount: 0 },
  ],
  medical_expenses: [
    { id: 20, journal_entry_id: 5, date: "2026-02-10", patient_name: "本人",
      hospital_name: "○○医院", treatment_description: "診察",
      provider_type: "hospital", amount_paid: 3000, insurance_reimbursement: 0 },
    { id: 21, journal_entry_id: 6, _decryptError: "bad" },
  ],
  vouchers: [
    { id: 30, journal_entry_id: 1, file_hash: "abc", file_size: 1234,
      uploaded_at: "2026-03-01T00:00:00Z" },
    { id: 31, journal_entry_id: 2, file_hash: "def", file_size: 0,
      _imageError: "not found" },
  ],
};


test("buildJournalCsv: 明細 1 行 = CSV 1 行、科目名解決", () => {
  const csv = buildJournalCsv(SAMPLE);
  const lines = csv.trimEnd().split("\r\n");
  // ヘッダ + 明細 3 行
  assert.equal(lines.length, 4);
  assert.match(lines[0], /^仕訳ID,日付,伝票番号,摘要,source,年度,月,科目コード,科目名,借方金額,貸方金額$/);
  // line 10: entry1, 科目 5010=消耗品費
  assert.match(lines[1], /^1,2026-03-01,1,ノート購入,journal,2026,3,5010,消耗品費,500,0$/);
  // line 11: entry1 貸方
  assert.match(lines[2], /1010,現金,0,500$/);
});

test("buildJournalCsv: 復号失敗の entry は (復号失敗)、未知科目は空", () => {
  const csv = buildJournalCsv(SAMPLE);
  const lines = csv.trimEnd().split("\r\n");
  // line 12: entry2 (復号失敗) + 科目 9999 (名称なし)
  assert.equal(lines[3], "2,(復号失敗),2,(復号失敗),(復号失敗),2026,3,9999,,100,0");
});

test("buildAccountsCsv: 有効フラグと廃止年", () => {
  const csv = buildAccountsCsv(SAMPLE);
  const lines = csv.trimEnd().split("\r\n");
  assert.equal(lines.length, 3);
  assert.match(lines[1], /^1010,現金,.*,1,$/);
  assert.match(lines[2], /^5010,消耗品費,備品,課税,expense,,0,2024$/);
});

test("buildMedicalCsv: body 値と復号失敗", () => {
  const csv = buildMedicalCsv(SAMPLE);
  const lines = csv.trimEnd().split("\r\n");
  assert.equal(lines.length, 3);
  assert.match(lines[1], /^5,2026-02-10,本人,○○医院,診察,hospital,3000,0$/);
  assert.equal(lines[2], "6,(復号失敗),(復号失敗),(復号失敗),(復号失敗),(復号失敗),(復号失敗),(復号失敗)");
});

test("buildVouchersCsv: 画像ファイル名解決と取得失敗マーク", () => {
  const names = new Map([[30, "voucher_30.jpg"]]);
  const csv = buildVouchersCsv(SAMPLE, names);
  const lines = csv.trimEnd().split("\r\n");
  assert.equal(lines.length, 3);
  assert.match(lines[1], /^30,1,voucher_30\.jpg,abc,1234,/);
  assert.match(lines[2], /^31,2,\(取得失敗\),def,0,$/);
});

test("空データでもヘッダのみ CSV を返す", () => {
  const empty = { accounts: [], journal_entries: [], journal_entry_lines: [],
    medical_expenses: [], vouchers: [] };
  assert.match(buildJournalCsv(empty), /^仕訳ID,.*\r\n$/);
  assert.match(buildAccountsCsv(empty), /^コード,.*\r\n$/);
  assert.match(buildMedicalCsv(empty), /^仕訳ID,.*\r\n$/);
  assert.match(buildVouchersCsv(empty), /^証憑ID,.*\r\n$/);
});
