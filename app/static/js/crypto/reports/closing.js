// #338 item1: 損益振替 (closing / 決算振替) 仕訳のクライアント暗号化生成。
//
// サーバは MK を持たないため、旧 fiscal.py:generate_closing_entries が行っていた
// 平文 SQL SUM による損益振替の生成をクライアントへ移譲する。クライアントは
// 全仕訳を復号して収益/費用の科目別残高を集計し (computeBalanceCache)、収益・費用を
// ゼロに振り戻して純損益を繰越利益 (retained_earnings) へ振り替える closing 仕訳を
// 組み立て、buildJournalEntry で暗号化して専用エンドポイント
// POST /api/v1/fiscal/close-closing へ送る。

import { fetchJournalsForYear } from "../journals_client.js";
import { buildJournalEntry } from "../entries_builder.js";
import { computeBalanceCache } from "./balance_cache.js";


/**
 * 収益/費用の科目別残高から損益振替 (closing) の明細行を構築する純粋関数。
 *
 * 旧サーバ実装 (fiscal.py:generate_closing_entries) と等価:
 *   - 収益科目 (type=revenue): 残高 = credit - debit。正なら借方計上 (ゼロ戻し)、
 *     負なら貸方計上。totalRevenue に加算。
 *   - 費用科目 (type=expense): 残高 = debit - credit。正なら貸方計上 (ゼロ戻し)、
 *     負なら借方計上。totalExpense に加算。
 *   - 純損益 net = totalRevenue - totalExpense を繰越利益へ: net>0 (利益) は貸方、
 *     net<0 (損失) は借方。net===0 は繰越利益行なし。
 * 収益費用の活動が無ければ空配列 (= 振替不要)。残高ゼロの科目はスキップ。
 *
 * @param {Object<string, [number, number]>} balanceCache
 *   computeBalanceCache の出力 {account_code: [debitSum, creditSum]}。
 * @param {Object<string, {type?: string, system_role?: string}>} accountsMeta
 *   科目コード → メタ (type=asset/liability/equity/revenue/expense, system_role)。
 * @returns {Array<{account_code: string, debit: number, credit: number}>}
 * @throws {Error} 収益費用活動があるのに繰越利益科目が無い場合。
 */
export function buildClosingLines(balanceCache, accountsMeta) {
  const meta = accountsMeta || {};
  let retainedCode = null;
  for (const code of Object.keys(meta)) {
    if (meta[code] && meta[code].system_role === "retained_earnings") {
      retainedCode = code;
      break;
    }
  }

  const lines = [];
  let totalRevenue = 0;
  let totalExpense = 0;

  for (const code of Object.keys(balanceCache || {})) {
    const m = meta[code];
    if (!m) continue;
    const pair = balanceCache[code] || [0, 0];
    const debit = pair[0] || 0;
    const credit = pair[1] || 0;
    if (m.type === "revenue") {
      const bal = credit - debit;  // 収益は貸方残高が正
      if (bal > 0) lines.push({ account_code: code, debit: bal, credit: 0 });
      else if (bal < 0) lines.push({ account_code: code, debit: 0, credit: -bal });
      totalRevenue += bal;
    } else if (m.type === "expense") {
      const bal = debit - credit;  // 費用は借方残高が正
      if (bal > 0) lines.push({ account_code: code, debit: 0, credit: bal });
      else if (bal < 0) lines.push({ account_code: code, debit: -bal, credit: 0 });
      totalExpense += bal;
    }
  }

  if (lines.length === 0) return [];  // 振替不要 (収益費用ゼロ)

  if (!retainedCode) {
    throw new Error("繰越利益 (retained_earnings) 科目が見つかりません。");
  }

  const net = totalRevenue - totalExpense;
  if (net > 0) lines.push({ account_code: retainedCode, debit: 0, credit: net });
  else if (net < 0) lines.push({ account_code: retainedCode, debit: -net, credit: 0 });

  return lines;
}


/**
 * 当年度の損益振替 closing 仕訳をクライアントで集計・暗号化し、専用エンドポイントへ
 * POST して決算月3 (period15) を確定する。
 *
 * @param {Object} args
 * @param {Object} args.client          SharedCryptoClient (MK 解錠済み前提)
 * @param {number|bigint} args.userId
 * @param {number} args.year
 * @param {Object} args.accountsMeta    {code: {type, system_role, ...}}
 * @param {Function} [args.fetchImpl]      テスト DI (fetchJournalsForYear へ渡す HTTP)
 * @param {Function} [args.fetchJournals]  テスト DI (default fetchJournalsForYear)
 * @param {Function} [args.buildImpl]      テスト DI (default buildJournalEntry)
 * @param {Function} [args.postImpl]       テスト DI (default globalThis.fetch)
 * @returns {Promise<Object>}  サーバ応答 {ok, closed_period, closing_entry_id}
 */
export async function buildAndPostClosingEntry({
  client, userId, year, accountsMeta,
  fetchImpl, fetchJournals = fetchJournalsForYear,
  buildImpl = buildJournalEntry, postImpl,
}) {
  const entries = await fetchJournals({
    client, userId, fiscalYear: year, fetchImpl,
  });
  // period:15 = 期首〜決算整理 (0-15) の全期間集計。includeClosing:false で
  // 既存 closing を除外し二重カウントを防ぐ (サーバ旧ロジックの全期間集計と等価)。
  const balances = computeBalanceCache(entries, {
    period: 15, includeClosing: false,
  });
  const lines = buildClosingLines(balances, accountsMeta);

  let closingEntry = null;
  if (lines.length > 0) {
    closingEntry = await buildImpl({
      client, userId,
      date: `${year}-12-31`,
      description: "損益振替仕訳（自動生成）",
      lines,
      source: "closing",
      fiscalPeriod: 16,
      _allowClosing: true,
    });
  }

  const post = postImpl ?? globalThis.fetch;
  // /api/v1 は CSRF 免除。session cookie は credentials:include で送る。
  const r = await post("/api/v1/fiscal/close-closing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ year, closing_entry: closingEntry }),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(e.error || `close-closing に失敗しました (HTTP ${r.status})`);
  }
  return r.json();
}
