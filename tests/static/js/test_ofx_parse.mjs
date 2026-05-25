// ofx_parse.js (Phase E3-D-2) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/ofx_parse.js",
  import.meta.url,
);
const { parseOfx } = await import(M.href);


// --- fixtures ---

// OFX 1.x SGML 形式 (閉じタグ付き、日本の銀行で一般的)
const OFX_SGML = `OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>JPY
<BANKACCTFROM>
<BANKID>0001
<ACCTID>1234567
<ACCTTYPE>SAVINGS
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260201
<DTEND>20260228
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260215
<TRNAMT>-1500
<FITID>TX001
<NAME>SUPERMARKET
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260225
<TRNAMT>250000
<FITID>TX002
<NAME>SALARY
<MEMO>March payroll
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>`;


// OFX 2.x XML 形式 (厳密 XML)
const OFX_XML = `<?xml version="1.0"?>
<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>JPY</CURDEF>
<BANKACCTFROM>
<BANKID>9999</BANKID>
<ACCTID>987654</ACCTID>
<ACCTTYPE>CHECKING</ACCTTYPE>
</BANKACCTFROM>
<BANKTRANLIST>
<STMTTRN>
<TRNTYPE>DEBIT</TRNTYPE>
<DTPOSTED>20260301</DTPOSTED>
<TRNAMT>-3000</TRNAMT>
<NAME>CONVENIENCE STORE</NAME>
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>`;


// --- tests ---

test("parseOfx: 入力型バリデーション", () => {
  assert.throws(() => parseOfx(123), /Uint8Array or string/);
  assert.throws(() => parseOfx(null), /Uint8Array or string/);
});

test("parseOfx: SGML 形式 (日本の銀行で一般的)", () => {
  const r = parseOfx(OFX_SGML);
  assert.equal(r.account_id, "1234567");
  assert.equal(r.account_type, "SAVINGS");
  assert.equal(r.rows.length, 2);
  // 1 行目: 出金 1500
  assert.equal(r.rows[0].row_num, 1);
  assert.equal(r.rows[0].date, "2026-02-15");
  assert.equal(r.rows[0].description, "SUPERMARKET");
  assert.equal(r.rows[0].deposit, 0);
  assert.equal(r.rows[0].withdrawal, 1500);
  // 2 行目: 入金 250000 + memo
  assert.equal(r.rows[1].date, "2026-02-25");
  assert.equal(r.rows[1].description, "SALARY March payroll");
  assert.equal(r.rows[1].deposit, 250000);
  assert.equal(r.rows[1].withdrawal, 0);
});

test("parseOfx: XML 形式 (OFX 2.x)", () => {
  const r = parseOfx(OFX_XML);
  assert.equal(r.account_id, "987654");
  assert.equal(r.account_type, "CHECKING");
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].date, "2026-03-01");
  assert.equal(r.rows[0].description, "CONVENIENCE STORE");
  assert.equal(r.rows[0].withdrawal, 3000);
});

test("parseOfx: Uint8Array 入力", () => {
  const bytes = new TextEncoder().encode(OFX_XML);
  const r = parseOfx(bytes);
  assert.equal(r.rows.length, 1);
});

test("parseOfx: 空 OFX (取引なし) で rows=[]", () => {
  const empty = `<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<BANKACCTFROM>
<ACCTID>1
<ACCTTYPE>SAVINGS
</BANKACCTFROM>
<BANKTRANLIST>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>`;
  const r = parseOfx(empty);
  assert.equal(r.account_id, "1");
  assert.deepEqual(r.rows, []);
});

test("parseOfx: 完全に空の string", () => {
  const r = parseOfx("");
  assert.deepEqual(r, { account_id: "", account_type: "", rows: [] });
});

test("parseOfx: payee と memo が同一なら重複除去", () => {
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260101
<TRNAMT>100
<NAME>SAME
<MEMO>SAME
</STMTTRN>
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows[0].description, "SAME");  // 重複除去
});

test("parseOfx: PAYEE タグも NAME 代替として認識", () => {
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260101
<TRNAMT>500
<PAYEE>FOO BANK
</STMTTRN>
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows[0].description, "FOO BANK");
});

test("parseOfx: 小数金額は切り捨て", () => {
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260101
<TRNAMT>-1500.99
<NAME>TEST
</STMTTRN>
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows[0].withdrawal, 1500);  // -1500.99 → -1500 → 1500
});

test("parseOfx: TRNAMT 空/不正で 0", () => {
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260101
<TRNAMT>
<NAME>EMPTY
</STMTTRN>
<STMTTRN>
<DTPOSTED>20260102
<TRNAMT>abc
<NAME>BAD
</STMTTRN>
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows[0].deposit, 0);
  assert.equal(r.rows[0].withdrawal, 0);
  assert.equal(r.rows[1].deposit, 0);
});

test("parseOfx: DTPOSTED に時刻付き (YYYYMMDDHHMMSS) も対応", () => {
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260101120000
<TRNAMT>100
<NAME>TIMED
</STMTTRN>
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows[0].date, "2026-01-01");  // 時刻部分は無視
});

test("parseOfx: DTPOSTED 不正で null", () => {
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260230
<TRNAMT>100
<NAME>BAD DATE
</STMTTRN>
<STMTTRN>
<DTPOSTED>
<TRNAMT>200
<NAME>NO DATE
</STMTTRN>
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows[0].date, null);  // 2/30 → null
  assert.equal(r.rows[1].date, null);  // 空 → null
});

test("parseOfx: SGML 閉じタグなし形式", () => {
  // 閉じタグ </STMTTRN> がない SGML 形式
  const ofx = `<OFX><BANKTRANLIST>
<STMTTRN>
<DTPOSTED>20260101
<TRNAMT>100
<NAME>A
<STMTTRN>
<DTPOSTED>20260102
<TRNAMT>200
<NAME>B
</BANKTRANLIST></OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.rows.length, 2);
  assert.equal(r.rows[0].description, "A");
  assert.equal(r.rows[1].description, "B");
});

test("parseOfx: ACCTID / ACCTTYPE 欠落で空文字", () => {
  const ofx = `<OFX>
<BANKMSGSRSV1>
<BANKTRANLIST>
</BANKTRANLIST>
</BANKMSGSRSV1>
</OFX>`;
  const r = parseOfx(ofx);
  assert.equal(r.account_id, "");
  assert.equal(r.account_type, "");
});

test("parseOfx: OFX ヘッダ (KEY:VALUE) 行をスキップ", () => {
  const r = parseOfx(OFX_SGML);
  // 'OFXHEADER:100' 等が ACCTID に紛れ込まないこと
  assert.equal(r.account_id, "1234567");
});

test("parseOfx: SGML 閉じタグなし + 親閉じタグも欠落でも text.length を end に", () => {
  // </BANKTRANLIST> も </STMTRS> もない異常な OFX。
  // indexOf == -1 が || で truthy として扱われてしまう旧バグの回帰テスト。
  // 修正後: 最後の STMTTRN は text.length までを description として取得する。
  const ofx = `<OFX>
<STMTTRN>
<DTPOSTED>20260101
<TRNAMT>100
<NAME>FIRST
<STMTTRN>
<DTPOSTED>20260102
<TRNAMT>200
<NAME>LAST`;
  const r = parseOfx(ofx);
  assert.equal(r.rows.length, 2);
  assert.equal(r.rows[0].description, "FIRST");
  assert.equal(r.rows[1].description, "LAST");
});
