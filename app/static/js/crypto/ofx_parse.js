// Phase E3-D-2: OFX 明細パースのクライアントサイド純粋関数。
//
// OFX (Open Financial Exchange) 1.x SGML 形式と 2.x XML 形式の両方に
// 対応する最小実装。STMTTRN ブロックから取引行を抽出して CSV/Web と
// 同じ {row_num, date, description, deposit, withdrawal} 形式で返す。
//
// サーバ側 app/services/ofx_import.py (ofxparse ライブラリ依存) と並存。
// 外部ライブラリ依存なし (正規表現ベース)。UI 統合は別 PR (E3-D-2b)、
// サーバ側削除は Phase E7 (E3-F)。


/**
 * OFX ファイルのバイト列をパース。
 *
 * @param {Uint8Array|string} input
 * @returns {{account_id: string, account_type: string,
 *   rows: Array<{row_num, date, description, deposit, withdrawal}>}}
 */
export function parseOfx(input) {
  let text;
  if (typeof input === "string") {
    text = input;
  } else if (input instanceof Uint8Array) {
    // OFX は通常 USASCII / UTF-8 / SJIS のいずれか。
    // ENCODING:USASCII / ENCODING:UTF-8 等のヘッダで判別したいが、
    // 日本の銀行は USASCII 限定の OFX が大半なので utf-8 fallback で十分。
    text = new TextDecoder("utf-8").decode(input);
  } else {
    throw new TypeError("input must be Uint8Array or string");
  }

  // OFX ヘッダ部分 (XML 化以前の `KEY:VALUE` 行群) をスキップ。
  // OFX 1.x: 行頭 `OFXHEADER:100` 等が並び、空行の後に <OFX> が始まる。
  // OFX 2.x: 先頭が <?xml version="1.0"?> の XML。
  const ofxStart = text.indexOf("<OFX>");
  const body = ofxStart >= 0 ? text.slice(ofxStart) : text;

  const account_id = _extractTag(body, "ACCTID");
  const account_type = _extractTag(body, "ACCTTYPE");

  // STMTTRN ブロックを抽出。SGML 形式は </STMTTRN> がない場合もあるが
  // 日本の OFX は概ね閉じタグ付き。XML 形式も同じ。
  // 閉じタグなしの SGML 1.x にも対応するため、次の <STMTTRN> または
  // 親要素閉じタグまでで区切る。
  const txns = _extractStmtTrns(body);

  const rows = [];
  for (let i = 0; i < txns.length; i++) {
    const tx = txns[i];
    const payee = _extractTag(tx, "NAME") || _extractTag(tx, "PAYEE");
    const memo = _extractTag(tx, "MEMO");
    const dtPosted = _extractTag(tx, "DTPOSTED");
    const trnAmt = _extractTag(tx, "TRNAMT");

    // description: payee と memo を結合 (重複除去)
    const parts = [];
    if (payee) parts.push(payee.trim());
    if (memo && memo.trim() !== payee?.trim()) parts.push(memo.trim());
    const description = parts.join(" ");

    // amount: 正=入金、負=出金
    const amount = _parseOfxAmount(trnAmt);
    const deposit = amount > 0 ? amount : 0;
    const withdrawal = amount < 0 ? Math.abs(amount) : 0;

    rows.push({
      row_num: i + 1,
      date: _parseOfxDate(dtPosted),
      description,
      deposit,
      withdrawal,
    });
  }

  return {
    account_id: account_id || "",
    account_type: account_type || "",
    rows,
  };
}


// OFX タグから値を抽出 (SGML/XML 両対応)。
// SGML: <TAG>value\n<NEXTTAG>...   (閉じタグなし、改行 or 次タグで終端)
// XML:  <TAG>value</TAG>            (明示閉じタグ)
function _extractTag(text, tag) {
  // まず XML 形式の明示閉じタグを試す
  const xmlRe = new RegExp(`<${tag}>([^<]*)</${tag}>`, "i");
  const xmlMatch = text.match(xmlRe);
  if (xmlMatch) return xmlMatch[1].trim();

  // SGML 形式: <TAG>value (改行 or 次タグで終端)
  const sgmlRe = new RegExp(`<${tag}>([^<\\r\\n]*)`, "i");
  const sgmlMatch = text.match(sgmlRe);
  if (sgmlMatch) return sgmlMatch[1].trim();

  return "";
}


// <STMTTRN>...</STMTTRN> または <STMTTRN>...(次の <STMTTRN> 直前まで) を
// すべて抽出して文字列配列で返す。
function _extractStmtTrns(text) {
  const result = [];

  // 閉じタグ付きを優先 (XML 形式)
  let m;
  const closedRe = /<STMTTRN>([\s\S]*?)<\/STMTTRN>/gi;
  while ((m = closedRe.exec(text)) !== null) {
    result.push(m[1]);
  }
  if (result.length > 0) return result;

  // 閉じタグなし SGML 形式: <STMTTRN> から次の <STMTTRN> または親閉じタグまで
  const openRe = /<STMTTRN>/gi;
  const openMatches = [];
  while ((m = openRe.exec(text)) !== null) {
    openMatches.push(m.index);
  }
  for (let i = 0; i < openMatches.length; i++) {
    const start = openMatches[i] + "<STMTTRN>".length;
    let end;
    if (i + 1 < openMatches.length) {
      end = openMatches[i + 1];
    } else {
      // 親閉じタグを探す。indexOf は見つからない時 -1 を返す
      // (truthy なので `||` で扱うとバグになる、明示的に分岐)。
      const bankTranEnd = text.indexOf("</BANKTRANLIST>", start);
      const stmtrsEnd = text.indexOf("</STMTRS>", start);
      if (bankTranEnd >= 0) end = bankTranEnd;
      else if (stmtrsEnd >= 0) end = stmtrsEnd;
      else end = text.length;
    }
    if (end > start) {
      result.push(text.slice(start, end));
    }
  }
  return result;
}


// OFX 金額文字列 → integer (小数は切り捨て、空は 0)
function _parseOfxAmount(s) {
  if (!s) return 0;
  const trimmed = String(s).trim();
  if (trimmed === "" || trimmed === "-") return 0;
  const n = parseFloat(trimmed);
  if (!Number.isFinite(n)) return 0;
  return Math.trunc(n);
}


// OFX 日付 ("YYYYMMDD" or "YYYYMMDDHHMMSS" or "YYYYMMDDHHMMSS.XXX[TZ]")
// → "YYYY-MM-DD" 文字列 (失敗時 null)。
function _parseOfxDate(s) {
  if (!s) return null;
  const trimmed = String(s).trim();
  // 先頭 8 文字 = YYYYMMDD
  const m = trimmed.match(/^(\d{4})(\d{2})(\d{2})/);
  if (!m) return null;
  const year = parseInt(m[1], 10);
  const month = parseInt(m[2], 10);
  const day = parseInt(m[3], 10);
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  // 月末日チェック
  const d = new Date(Date.UTC(year, month - 1, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1
      || d.getUTCDate() !== day) {
    return null;
  }
  return `${m[1]}-${m[2]}-${m[3]}`;
}
