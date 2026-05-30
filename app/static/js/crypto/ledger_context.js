// 元帳コンテキストのクライアント側構築 (E3-F PR-D-6-1)。
//
// 旧サーバ側 ai_receipt._get_payment_ledger_context と等価。サーバは平文
// JournalEntry.date / JournalEntry.description を読んで「支払口座の元帳テキスト
// (相手科目名つき)」を組み立て、suggest-categories の LLM プロンプトに埋め込んで
// いた。E2EE 化 (E3-F PR-D-6) ではこの平文読取を撤去するため、復号済み仕訳を
// 入力に取る純粋関数としてクライアントへ移植する。
//
// 入力の journalEntries は journals_client.fetchJournalsForYear が返す正規化
// 形式 ({id, date, description, lines: [{account_code, debit, credit}]})。

/**
 * 金額を "1,234" 形式 (en-US カンマ区切り) に整形する。
 * サーバ側 f"{d:,}" と等価。
 */
function _fmtYen(n) {
  return Number(n || 0).toLocaleString("en-US");
}


/**
 * 支払口座の元帳テキストを組み立てる (相手科目名つき)。
 *
 * サーバ側 _get_payment_ledger_context の忠実な移植:
 *   - paymentAccountCode の明細を持つ仕訳を date desc (tie は id desc) で
 *     最大 limit 件抽出
 *   - 各行: "日付 | 摘要 | 相手科目 | 入金 | 出金"
 *     - 相手科目 = 同一仕訳の paymentAccountCode 以外の明細の科目名を ", " 連結
 *       (accountNameMap で解決、解決できなければ "?")
 *     - 入金 = debit が正なら "¥{debit}"、0 なら "-"
 *     - 出金 = credit が正なら "¥{credit}"、0 なら "-"
 *
 * @param {Object} args
 * @param {string} args.accountName             支払口座の名前 (ヘッダ表示用)
 * @param {string} args.paymentAccountCode      支払口座コード
 * @param {Array<Object>} args.journalEntries   復号済み仕訳 (fetchJournalsForYear 形式)
 * @param {Object} args.accountNameMap          {code: name} (相手科目名解決用)
 * @param {number} [args.limit=100]             最大件数
 * @returns {string}                            整形済テキスト。account が無ければ ""、
 *                                              仕訳が無ければ "(元帳データなし)"
 */
export function buildPaymentLedgerContext({
  accountName, paymentAccountCode, journalEntries, accountNameMap, limit = 100,
}) {
  if (!paymentAccountCode) return "";
  if (!Array.isArray(journalEntries)) {
    throw new Error("journalEntries must be an array");
  }
  const nameMap = accountNameMap || {};

  // paymentAccountCode の明細を含む仕訳のみ抽出し、その明細 (借方/貸方額) を
  // 行として展開する。サーバは JournalEntryLine 単位で SELECT しているため
  // 1 仕訳に対象口座の明細が複数あれば複数行になる (通常は 1 行)。
  const rows = [];
  for (const entry of journalEntries) {
    if (!entry || !Array.isArray(entry.lines)) continue;
    for (const line of entry.lines) {
      if (line?.account_code !== paymentAccountCode) continue;
      rows.push({ entry, line });
    }
  }

  // date desc, id desc でソート。date は "YYYY-MM-DD" 文字列なので辞書順比較で
  // 日付順になる。date 欠落は最後尾へ。
  rows.sort((a, b) => {
    const da = a.entry?.date || "";
    const db = b.entry?.date || "";
    if (da !== db) return da < db ? 1 : -1; // desc
    const ia = Number(a.entry?.id) || 0;
    const ib = Number(b.entry?.id) || 0;
    return ib - ia; // id desc
  });

  const limited = rows.slice(0, limit);

  if (limited.length === 0) return "(元帳データなし)";

  const out = [`【${accountName ?? ""}】の元帳（直近${limited.length}件）`];
  out.push("日付 | 摘要 | 相手科目 | 入金 | 出金");
  out.push("-".repeat(60));

  for (const { entry, line } of limited) {
    const counterNames = [];
    for (const l of entry.lines) {
      if (l?.account_code === paymentAccountCode) continue;
      const name = nameMap[l?.account_code];
      if (name) counterNames.push(name);
    }
    const counter = counterNames.length > 0 ? counterNames.join(", ") : "?";

    const d = Math.trunc(Number(line.debit) || 0);
    const c = Math.trunc(Number(line.credit) || 0);
    out.push(
      `${entry.date} | ${entry.description ?? ""} | ${counter} | `
      + `${d ? "¥" + _fmtYen(d) : "-"} | `
      + `${c ? "¥" + _fmtYen(c) : "-"}`,
    );
  }

  return out.join("\n");
}


/**
 * account_list_text ("  1010 現金\n  5010 食費" 形式) を {code: name} の
 * マップにパースする。各行先頭の数字コード + 残りを科目名とみなす。
 * round2.js parseAccountCodes と同系だが name も拾う。
 *
 * @param {string} accountListText
 * @returns {Object} {code: name}
 */
export function parseAccountListText(accountListText) {
  const map = {};
  if (typeof accountListText !== "string") return map;
  for (const line of accountListText.split("\n")) {
    const m = line.trim().match(/^(\d+)\s+(.+)$/);
    if (m) map[m[1]] = m[2].trim();
  }
  return map;
}


/**
 * AI 証憑仕訳 Round 2 用の元帳テキストを科目別に組み立てる。
 *
 * 旧サーバ側 ai_receipt._get_ledger_context の移植。Round 1 LLM が要求した
 * 科目名 (accountNames) を全科目の名前と部分一致で解決し、各科目の元帳明細を
 * 出力する。
 *
 * サーバ実装との相違点:
 *   - 旧実装は各科目末尾に「累計: 借方合計 ¥X / 貸方合計 ¥Y」(全期間集計) を
 *     付けていたが、本実装では **累計行を出さない** (E3-F PR-D-6-1b でクライアントは
 *     全期間ではなく直近年度のみ復号取得するため、全期間累計を正確に出せない)。
 *   - 科目の解決順は accountNameMap の挿入順 (= account_list_text の記載順)。
 *
 * @param {Object} args
 * @param {string[]} args.accountNames           Round 1 LLM が要求した科目名
 * @param {Array<Object>} args.journalEntries    復号済み仕訳 (fetchJournalsForYear 形式)
 * @param {string} args.accountListText          "  1010 現金" 形式の科目一覧
 * @param {number} [args.limit=20]               科目あたりの明細件数 (旧サーバ同値)
 * @returns {string}                             整形済テキスト。該当科目なしなら ""
 */
export function buildAccountsLedgerContext({
  accountNames, journalEntries, accountListText, limit = 20,
}) {
  if (!Array.isArray(accountNames) || accountNames.length === 0) return "";
  if (!Array.isArray(journalEntries)) {
    throw new Error("journalEntries must be an array");
  }

  const codeToName = parseAccountListText(accountListText);

  // 科目名の部分一致で解決 (旧サーバ: name in acct.name or acct.name in name)。
  // codeToName の記載順 (= 科目一覧順) を保つため Object.entries を走査する。
  const matched = [];
  const seen = new Set();
  for (const [code, name] of Object.entries(codeToName)) {
    for (const reqName of accountNames) {
      if (typeof reqName !== "string" || !reqName) continue;
      if (name.includes(reqName) || reqName.includes(name)) {
        if (!seen.has(code)) {
          matched.push({ code, name });
          seen.add(code);
        }
        break;
      }
    }
  }

  if (matched.length === 0) return "";

  const out = [];
  for (const { code, name } of matched) {
    // 当該科目の明細を持つ仕訳を date desc / id desc で limit 件。
    const rows = [];
    for (const entry of journalEntries) {
      if (!entry || !Array.isArray(entry.lines)) continue;
      for (const line of entry.lines) {
        if (line?.account_code !== code) continue;
        rows.push({ entry, line });
      }
    }
    rows.sort((a, b) => {
      const da = a.entry?.date || "";
      const db = b.entry?.date || "";
      if (da !== db) return da < db ? 1 : -1; // desc
      const ia = Number(a.entry?.id) || 0;
      const ib = Number(b.entry?.id) || 0;
      return ib - ia; // id desc
    });
    const limited = rows.slice(0, limit);
    if (limited.length === 0) continue;

    out.push(`\n【${name}】（${code}）`);
    out.push("日付 | 摘要 | 借方 | 貸方");
    out.push("-".repeat(50));
    for (const { entry, line } of limited) {
      const d = Math.trunc(Number(line.debit) || 0);
      const c = Math.trunc(Number(line.credit) || 0);
      out.push(
        `${entry.date} | ${entry.description ?? ""} | `
        + `${d ? "¥" + _fmtYen(d) : "-"} | `
        + `${c ? "¥" + _fmtYen(c) : "-"}`,
      );
    }
  }

  return out.join("\n");
}
