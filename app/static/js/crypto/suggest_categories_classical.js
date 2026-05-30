// 非AI 科目推定 (suggest-categories) のクライアント側 classical 実装。
//
// 旧サーバ側 journal.suggest_categories (`POST /journal/api/suggest-categories`)
// と等価。サーバは平文 JournalEntry.description / JournalEntry.date を読んで
// 「同一摘要の最新仕訳の相手科目」を返していた。E2EE 化 (E3-F PR-D-4) では
// これらの平文読取を撤去するため、復号済み仕訳を入力に取る純粋関数として
// クライアントへ移植する。
//
// 入力の journalEntries は journals_client.fetchJournalsForYear が返す正規化
// 形式 ({id, date, description, source, lines: [{account_code, debit, credit}]})。
//
// AI 科目推定 (LLM 利用) は別物で suggest_categories_orchestrator.js が担当する。


/**
 * 摘要ごとに「同一摘要の最新仕訳の相手科目」を推定する。
 *
 * サーバ側ロジックの忠実な移植:
 *   - 摘要が完全一致する仕訳のうち最新 (date desc, id desc) を採用
 *   - その仕訳の明細を順に見て、支払口座以外で **有効な** 科目を相手科目とする
 *   - accountNameMap は有効科目のみを含む (get_grouped_accounts は is_active=True
 *     のみ返すため、map に含まれること ⟺ 有効。サーバの account.is_active 判定と等価)
 *
 * @param {Object} args
 * @param {string[]} args.descriptions            推定したい摘要の配列 (重複・空文字可)
 * @param {string} [args.paymentAccountCode]      支払口座コード (相手科目から除外)
 * @param {Array<Object>} args.journalEntries     復号済み仕訳 (fetchJournalsForYear 形式)
 * @param {Object} args.accountNameMap            {code: name} (有効科目のみ)
 * @returns {Object}                              {description: {account_code, account_name}}
 */
export function suggestCategoriesByHistory({
  descriptions, paymentAccountCode, journalEntries, accountNameMap,
}) {
  if (!Array.isArray(descriptions)) {
    throw new Error("descriptions must be an array");
  }
  if (!Array.isArray(journalEntries)) {
    throw new Error("journalEntries must be an array");
  }
  const nameMap = accountNameMap || {};

  // 空文字を除いた一意な摘要。Set で重複排除 (サーバの set(...) と同等)。
  const uniqueDescs = Array.from(new Set(descriptions.filter((d) => d)));
  if (uniqueDescs.length === 0) return {};

  // date desc, id desc で 1 度だけソートし、各摘要の最初のマッチを最新仕訳とする。
  // date は "YYYY-MM-DD" 文字列なので辞書順比較で日付順になる。date 欠落
  // (null/undefined) の仕訳は最後尾に送る。
  const sorted = journalEntries.slice().sort((a, b) => {
    const da = a?.date || "";
    const db = b?.date || "";
    if (da !== db) return da < db ? 1 : -1; // desc
    const ia = Number(a?.id) || 0;
    const ib = Number(b?.id) || 0;
    return ib - ia; // id desc
  });

  const result = {};
  for (const desc of uniqueDescs) {
    const entry = sorted.find((e) => e?.description === desc);
    if (!entry) continue;
    const lines = Array.isArray(entry.lines) ? entry.lines : [];
    for (const line of lines) {
      const code = line?.account_code;
      if (code === null || code === undefined) continue;
      // 支払口座は相手科目から除外 (paymentAccountCode が falsy なら除外しない)
      if (paymentAccountCode && code === paymentAccountCode) continue;
      const name = nameMap[code];
      if (name) { // map に存在 = 有効科目
        result[desc] = { account_code: code, account_name: name };
        break;
      }
    }
  }
  return result;
}
