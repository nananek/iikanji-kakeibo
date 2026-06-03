// E6 (#113) 全データエクスポート PR-1: CSV 生成の純粋関数群。
//
// backup_export_client.js の `decryptBackup` が返す復号済みオブジェクト
// (out.data.*) を、人間可読 (Excel / 表計算) な CSV 文字列へ変換する。
// DOM / fetch / crypto に依存しない純ロジックなので Node 単体テスト可能。
//
// 暗号文 body 由来の値 (date / description / 金額等) は復号失敗時に
// `_decryptError` が立つ行がある。その場合は欠落を握りつぶさず
// `(復号失敗)` を出力して可視化する。

const DECRYPT_FAILED = "(復号失敗)";


/**
 * 1 セルを RFC 4180 に従ってエスケープする。
 * `"` `,` 改行 (LF/CR) のいずれかを含む値はダブルクォートで囲み、
 * 内部の `"` は `""` に二重化する。null/undefined は空文字。
 *
 * @param {*} value
 * @returns {string}
 */
export function escapeCell(value) {
  if (value === null || value === undefined) return "";
  const s = String(value);
  if (/[",\r\n]/.test(s)) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}


/**
 * ヘッダ行 + データ行を CSV 文字列にする。改行は CRLF (RFC 4180)。
 * BOM は付けない (呼び出し側が UTF-8 BOM を付与する)。
 *
 * @param {string[]} headers
 * @param {Array<Array<*>>} rows
 * @returns {string}
 */
export function toCsv(headers, rows) {
  const lines = [headers.map(escapeCell).join(",")];
  for (const row of rows) {
    lines.push(row.map(escapeCell).join(","));
  }
  return lines.join("\r\n") + "\r\n";
}


/** accounts 配列から code -> name の Map を作る。 */
export function buildAccountNameMap(accounts) {
  const m = new Map();
  for (const a of accounts || []) {
    m.set(a.code, a.name);
  }
  return m;
}


/** 暗号文 body 由来の値を取り出す。復号失敗行は DECRYPT_FAILED を返す。 */
function _bodyVal(row, key) {
  if (row && row._decryptError) return DECRYPT_FAILED;
  const v = row ? row[key] : undefined;
  return v === undefined || v === null ? "" : v;
}


/**
 * 仕訳帳 CSV。journal_entries × journal_entry_lines を結合し、
 * 明細 1 行を CSV 1 行にする (伝票レベル列は反復)。
 *
 * 列: 仕訳ID, 日付, 伝票番号, 摘要, source, 年度, 月, 科目コード, 科目名,
 *     借方金額, 貸方金額
 */
export function buildJournalCsv(data) {
  const accountNames = buildAccountNameMap(data.accounts);
  const entryById = new Map();
  for (const e of data.journal_entries || []) {
    entryById.set(e.id, e);
  }
  const headers = [
    "仕訳ID", "日付", "伝票番号", "摘要", "source", "年度", "月",
    "科目コード", "科目名", "借方金額", "貸方金額",
  ];
  const rows = [];
  for (const l of data.journal_entry_lines || []) {
    const e = entryById.get(l.journal_entry_id);
    rows.push([
      l.journal_entry_id,
      _bodyVal(e, "date"),
      e ? (e.entry_number ?? "") : "",
      _bodyVal(e, "description"),
      _bodyVal(e, "source"),
      e ? (e.fiscal_year ?? "") : "",
      e ? (e.fiscal_month ?? "") : "",
      l.account_code ?? "",
      accountNames.get(l.account_code) ?? "",
      l.debit_amount ?? 0,
      l.credit_amount ?? 0,
    ]);
  }
  return toCsv(headers, rows);
}


/**
 * 勘定科目マスタ CSV。
 * 列: コード, 名称, 説明, 税区分, 原価区分, system_role, 有効, 廃止年
 */
export function buildAccountsCsv(data) {
  const headers = [
    "コード", "名称", "説明", "税区分", "原価区分", "system_role",
    "有効", "廃止年",
  ];
  const rows = [];
  for (const a of data.accounts || []) {
    rows.push([
      a.code ?? "",
      a.name ?? "",
      a.description ?? "",
      a.tax_category ?? "",
      a.cost_type ?? "",
      a.system_role ?? "",
      a.is_active ? "1" : "0",
      a.deactivated_year ?? "",
    ]);
  }
  return toCsv(headers, rows);
}


/**
 * 医療費 CSV。値は暗号文 body 由来 (復号失敗時 (復号失敗))。
 * 列: 仕訳ID, 日付, 受診者, 医療機関, 内容, 区分, 支払額, 補填額
 */
export function buildMedicalCsv(data) {
  const headers = [
    "仕訳ID", "日付", "受診者", "医療機関", "内容", "区分",
    "支払額", "補填額",
  ];
  const rows = [];
  for (const m of data.medical_expenses || []) {
    rows.push([
      m.journal_entry_id ?? "",
      _bodyVal(m, "date"),
      _bodyVal(m, "patient_name"),
      _bodyVal(m, "hospital_name"),
      _bodyVal(m, "treatment_description"),
      _bodyVal(m, "provider_type"),
      _bodyVal(m, "amount_paid"),
      _bodyVal(m, "insurance_reimbursement"),
    ]);
  }
  return toCsv(headers, rows);
}


/**
 * 証憑メタデータ CSV (画像本体は zip の vouchers/ に別途格納)。
 * 列: 証憑ID, 仕訳ID, ファイル名, file_hash, サイズ(bytes), アップロード日時
 *
 * @param {Object} data
 * @param {Map<number,string>} [imageNames] voucher.id -> zip 内ファイル名
 */
export function buildVouchersCsv(data, imageNames) {
  const headers = [
    "証憑ID", "仕訳ID", "ファイル名", "file_hash", "サイズ(bytes)",
    "アップロード日時",
  ];
  const rows = [];
  for (const v of data.vouchers || []) {
    const name = imageNames && imageNames.get(v.id);
    rows.push([
      v.id ?? "",
      v.journal_entry_id ?? "",
      name ?? (v._imageError ? "(取得失敗)" : ""),
      v.file_hash ?? "",
      v.file_size ?? "",
      v.uploaded_at ?? "",
    ]);
  }
  return toCsv(headers, rows);
}
