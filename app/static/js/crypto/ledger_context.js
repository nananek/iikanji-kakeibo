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
