// Phase E3-D-1: CSV 明細パースのクライアントサイド純粋関数。
//
// 銀行・クレカ CSV をブラウザ側で完全にパースし、サーバには平文を送らず
// 暗号化済の仕訳だけを送信する。E2EE の主目的 (生データのサーバ通過防止)
// に直結する。
//
// サーバ側 app/services/csv_import.py と並存。UI 統合は別 PR (E3-D-1b)、
// サーバ側削除は Phase E7 (E3-F)。
//
// 設計書 §12.5 参照。


// 日本の銀行・クレカ CSV で使われる主要な日付フォーマット (Python strftime)
export const DATE_FORMATS = [
  ["YYYY/MM/DD", "%Y/%m/%d"],
  ["YYYY-MM-DD", "%Y-%m-%d"],
  ["YYYY年MM月DD日", "%Y年%m月%d日"],
  ["YY/MM/DD", "%y/%m/%d"],
  ["MM/DD/YYYY", "%m/%d/%Y"],
];


/**
 * バイト列のエンコーディングを推定。
 *
 * TextDecoder の fatal=true で例外を投げる試行を順に行い、最初に成功した
 * エンコーディングを返す。日本の家計簿用途を想定し utf-8 / shift_jis /
 * euc-jp の順で試行。
 *
 * @param {Uint8Array} bytes
 * @returns {string}  IANA エンコーディング名 (TextDecoder で使用可)
 */
export function detectEncoding(bytes) {
  // UTF-8 BOM 付きは utf-8 として確定
  if (bytes.length >= 3 && bytes[0] === 0xEF && bytes[1] === 0xBB && bytes[2] === 0xBF) {
    return "utf-8";
  }
  const candidates = ["utf-8", "shift_jis", "euc-jp"];
  for (const enc of candidates) {
    try {
      new TextDecoder(enc, { fatal: true }).decode(bytes);
      return enc;
    } catch (_e) {
      // 次の候補へ
    }
  }
  return "utf-8";  // 最終フォールバック (非 fatal で置換文字を許容)
}


// CSV パーサ: ダブルクォート対応の最小実装 (RFC 4180 サブセット)。
// PapaParse 等の外部ライブラリは依存追加を避けるため自前実装。
function _parseCsvText(text) {
  // BOM 除去
  if (text.charCodeAt(0) === 0xFEFF) text = text.slice(1);
  const rows = [];
  let row = [];
  let field = "";
  let inQuote = false;
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    if (inQuote) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuote = false;
        i++;
      } else {
        field += c;
        i++;
      }
    } else {
      if (c === '"') {
        inQuote = true;
        i++;
      } else if (c === ",") {
        row.push(field);
        field = "";
        i++;
      } else if (c === "\r") {
        // CRLF or CR single
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
        i++;
        if (text[i] === "\n") i++;
      } else if (c === "\n") {
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
        i++;
      } else {
        field += c;
        i++;
      }
    }
  }
  // 末尾行 (改行で終わらない場合)
  if (field !== "" || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}


/**
 * CSV プレビュー (ヘッダ + 先頭 N 行)。
 *
 * @param {Uint8Array} bytes
 * @param {Object} [options]
 * @param {number} [options.maxRows=20]
 * @returns {{encoding, headers: string[], rows: string[][], total_rows: number}}
 */
export function parseCsvPreview(bytes, options = {}) {
  const { maxRows = 20 } = options;
  if (!(bytes instanceof Uint8Array)) {
    throw new TypeError("bytes must be a Uint8Array");
  }
  const encoding = detectEncoding(bytes);
  const text = new TextDecoder(encoding).decode(bytes);
  const allRows = _parseCsvText(text).filter(
    (r) => r.some((cell) => cell.trim() !== ""),
  );
  if (allRows.length === 0) {
    return { encoding, headers: [], rows: [], total_rows: 0 };
  }
  const headers = allRows[0];
  const dataRows = allRows.slice(1);
  return {
    encoding,
    headers,
    rows: dataRows.slice(0, maxRows),
    total_rows: dataRows.length,
  };
}


/**
 * 金額文字列をパースして整数を返す (符号保持)。
 *
 * 対応: "1,234" / "¥1,234" / "￥1,234" / "-500" / "1234円" / 空 → 0
 * マイナス値はそのまま返す (呼び出し側で振替処理を行う)。
 *
 * @param {string|null|undefined} value
 * @returns {number}
 */
export function parseAmount(value) {
  if (value == null) return 0;
  let s = String(value).trim();
  if (s === "") return 0;
  s = s.replace(/,/g, "")
       .replace(/¥/g, "")
       .replace(/￥/g, "")
       .replace(/円/g, "")
       .replace(/¥/g, "")  // half-width yen
       .trim();
  if (s === "" || s === "-") return 0;
  const n = parseFloat(s);
  if (!Number.isFinite(n)) return 0;
  return Math.trunc(n);
}


// strftime → 正規表現マップ
const STRFTIME_TO_REGEX = {
  "%Y": "(?<Y>\\d{4})",
  "%y": "(?<y>\\d{2})",
  "%m": "(?<m>\\d{1,2})",
  "%d": "(?<d>\\d{1,2})",
};


function _strftimeToRegex(fmt) {
  // ", /, -, 年, 月, 日 等はそのまま (エスケープ)
  let regex = "";
  let i = 0;
  while (i < fmt.length) {
    const c = fmt[i];
    if (c === "%" && i + 1 < fmt.length) {
      const token = fmt.slice(i, i + 2);
      const replace = STRFTIME_TO_REGEX[token];
      if (replace) {
        regex += replace;
        i += 2;
        continue;
      }
    }
    // 正規表現メタ文字をエスケープ
    regex += c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    i++;
  }
  return new RegExp("^" + regex + "$");
}


/**
 * 日付文字列をパースして "YYYY-MM-DD" 文字列を返す (失敗時 null)。
 *
 * @param {string|null|undefined} value
 * @param {string} formatStr  Python strftime ("%Y/%m/%d" 等)
 * @returns {string|null}     ISO date string or null
 */
export function parseDate(value, formatStr) {
  if (value == null) return null;
  const s = String(value).trim();
  if (s === "") return null;
  // まず指定フォーマットを試す
  const formatted = _tryParse(s, formatStr);
  if (formatted) return formatted;
  // フォールバック: 全フォーマットを試す
  for (const [, fmt] of DATE_FORMATS) {
    const r = _tryParse(s, fmt);
    if (r) return r;
  }
  return null;
}


function _tryParse(str, fmt) {
  const re = _strftimeToRegex(fmt);
  const m = str.match(re);
  if (!m) return null;
  let year, month, day;
  if (m.groups.Y) year = parseInt(m.groups.Y, 10);
  else if (m.groups.y) {
    const yy = parseInt(m.groups.y, 10);
    // 2 桁年: 00..68 → 2000s, 69..99 → 1900s (Python strptime と同じ)
    year = yy < 69 ? 2000 + yy : 1900 + yy;
  } else return null;
  month = parseInt(m.groups.m, 10);
  day = parseInt(m.groups.d, 10);
  if (month < 1 || month > 12) return null;
  if (day < 1 || day > 31) return null;
  // 月末日チェック (簡易: 30/31 / うるう年)
  const d = new Date(Date.UTC(year, month - 1, day));
  if (d.getUTCFullYear() !== year || d.getUTCMonth() !== month - 1
      || d.getUTCDate() !== day) {
    return null;
  }
  return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}


/**
 * CSV をフルパースして取込用データを返す。
 *
 * @param {Uint8Array} bytes
 * @param {Object} mapping
 * @param {number} mapping.date_col
 * @param {number} mapping.desc_col
 * @param {number|null} [mapping.deposit_col]
 * @param {number|null} [mapping.withdrawal_col]
 * @param {string} dateFormatStr Python strftime
 *
 * @returns {Array<{row_num, date: string|null, description: string,
 *   deposit: number, withdrawal: number, raw_row: string[]}>}
 *
 * 振り分け規則:
 *   - deposit_col, withdrawal_col 両方 None の行は無視 (date も無効)
 *   - マイナス値は自動反転して逆側に振り分け
 *     (出金 -500 → 入金 500、クレカのキャッシュバック等)
 */
export function parseCsvFull(bytes, mapping, dateFormatStr) {
  if (!(bytes instanceof Uint8Array)) {
    throw new TypeError("bytes must be a Uint8Array");
  }
  if (!mapping || typeof mapping.date_col !== "number"
      || typeof mapping.desc_col !== "number") {
    throw new TypeError("mapping.date_col and mapping.desc_col are required");
  }
  const encoding = detectEncoding(bytes);
  const text = new TextDecoder(encoding).decode(bytes);
  const allRows = _parseCsvText(text).filter(
    (r) => r.some((cell) => cell.trim() !== ""),
  );
  if (allRows.length < 2) return [];
  const dataRows = allRows.slice(1);  // ヘッダ行スキップ

  const { date_col, desc_col, deposit_col, withdrawal_col } = mapping;
  const results = [];

  for (let i = 0; i < dataRows.length; i++) {
    const row = dataRows[i];

    const safeGet = (idx) => {
      if (idx == null) return "";
      if (idx < 0 || idx >= row.length) return "";
      return (row[idx] ?? "").trim();
    };

    const parsedDate = parseDate(safeGet(date_col), dateFormatStr);
    const description = safeGet(desc_col);
    let deposit = 0;
    let withdrawal = 0;
    if (deposit_col != null) deposit = parseAmount(safeGet(deposit_col));
    if (withdrawal_col != null) withdrawal = parseAmount(safeGet(withdrawal_col));

    // マイナス値は反転 (クレカのキャッシュバック等)
    if (deposit < 0) {
      withdrawal += Math.abs(deposit);
      deposit = 0;
    }
    if (withdrawal < 0) {
      deposit += Math.abs(withdrawal);
      withdrawal = 0;
    }

    if (parsedDate == null && deposit === 0 && withdrawal === 0) continue;

    results.push({
      row_num: i + 2,  // 1-indexed, header=1
      date: parsedDate,
      description,
      deposit,
      withdrawal,
      raw_row: row,
    });
  }
  return results;
}
