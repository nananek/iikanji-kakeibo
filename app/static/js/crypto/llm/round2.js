// Round 2: Round 1 結果 + 元帳 → 仕訳案生成。
//
// サーバ側 ai_receipt.py analyze_and_suggest() の Round 2 部分と等価:
//   1. Round 1 結果 (needs_ledger / requested_accounts) を元に、呼出側が渡した
//      復号済み仕訳 (journalEntries) から元帳テキストをクライアント側で構築
//      (E3-F PR-D-6-1b: 旧 POST /api/v1/ai/ledger-context への依存を撤去)
//   2. prompt-context の round2_prompt_template_(no|with)_ledger を選択
//      → __ACCOUNT_LIST_TEXT__ / __LEDGER_TEXT__ を置換
//   3. callLLM で provider 別 LLM 呼出
//   4. レスポンス suggestions を account_code バリデーション → JournalSuggestion[]
//
// 戻り値: { suggestions, usage, raw, validCodeCount }

import { callLLM } from "./index.js";
import { buildAccountsLedgerContext } from "../ledger_context.js";


/**
 * account_list_text から有効な account_code 集合を抽出する。
 * テキスト形式は "  1010 現金\n  5010 食費\n" 等。各行先頭の数字部分。
 */
export function parseAccountCodes(accountListText) {
  if (typeof accountListText !== "string") return new Set();
  const codes = new Set();
  for (const line of accountListText.split("\n")) {
    // "  1010 現金" のような行から先頭の数字を抽出
    const m = line.trim().match(/^(\d+)\s/);
    if (m) codes.add(m[1]);
  }
  return codes;
}


/**
 * Round 2 プロンプトをテンプレートから組み立てる。
 * needs_ledger に応じてサーバが返した 2 テンプレートのどちらかを選び、
 * __ACCOUNT_LIST_TEXT__ / __LEDGER_TEXT__ をクライアント側で置換する。
 */
export function buildRound2Prompt({
  promptContext, needsLedger = false, ledgerText = "",
}) {
  if (!promptContext || typeof promptContext !== "object") {
    throw new Error("promptContext is required");
  }
  const accountListText = promptContext.account_list_text ?? "";
  const tpl = needsLedger
    ? promptContext.round2_prompt_template_with_ledger
    : promptContext.round2_prompt_template_no_ledger;
  if (typeof tpl !== "string") {
    throw new Error(
      `promptContext missing round2_prompt_template_${needsLedger ? "with" : "no"}_ledger`,
    );
  }
  let p = tpl.replaceAll("__ACCOUNT_LIST_TEXT__", accountListText);
  if (needsLedger) {
    p = p.replaceAll("__LEDGER_TEXT__", ledgerText || "");
  }
  return p;
}


/**
 * LLM の Round 2 戻り値 (`{ suggestions: [...] }`) を JournalSuggestion 形式に
 * 整形 + account_code バリデーションする。サーバ側 ai_receipt.py の
 * for s in suggestions_raw: ... ロジックと等価。
 *
 * 戻り値: { suggestions: 有効件, dropped: 無効件数 }
 *
 * 仕様: 借方 / 貸方の合計バランスチェックは行わない (line 数の有無のみ確認)。
 * サーバ側でも _build_suggestion_prompt は「合計一致」を LLM 出力の前提に
 * しているため、合計不一致は LLM のミスとして許容する設計。バランスの最終
 * 検証は仕訳登録フローの UI 側で実施する。
 */
export function validateSuggestions(rawSuggestions, validCodeSet) {
  if (!Array.isArray(rawSuggestions)) return { suggestions: [], dropped: 0 };
  const out = [];
  let dropped = 0;
  for (const s of rawSuggestions) {
    if (!s || typeof s !== "object") {
      dropped++;
      continue;
    }
    const linesRaw = Array.isArray(s.lines) ? s.lines : [];
    const lines = [];
    for (const line of linesRaw) {
      const acode = String(line?.account_code ?? "");
      if (!validCodeSet.has(acode)) continue;
      lines.push({
        account_code: acode,
        account_name: line.account_name ?? "",
        debit_amount: Number.isFinite(Number(line.debit_amount))
          ? Math.trunc(Number(line.debit_amount)) : 0,
        credit_amount: Number.isFinite(Number(line.credit_amount))
          ? Math.trunc(Number(line.credit_amount)) : 0,
      });
    }
    // 借方/貸方 lines が 1 件もなければ採用しない
    if (lines.length === 0) {
      dropped++;
      continue;
    }
    out.push({
      title: s.title ?? "",
      description: s.description ?? "",
      date: s.date ?? null,
      entry_description: s.entry_description ?? "",
      lines,
    });
  }
  return { suggestions: out, dropped };
}


/**
 * Round 2 を実行する。
 *
 * @param {Object} args
 * @param {Object} args.promptContext     /api/v1/ai/prompt-context レスポンス
 * @param {Object} args.round1Analysis    runRound1() の戻り値 analysis フィールド
 * @param {string} args.provider
 * @param {string} args.apiKey
 * @param {string} args.model
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType
 * @param {Array<Object>} [args.journalEntries]  復号済み仕訳 (元帳テキスト構築用)
 * @param {AbortSignal} [args.signal]
 * @param {Function} [args.callLLMImpl=callLLM]
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<{suggestions, usage, raw, dropped, ledgerText}>}
 */
export async function runRound2({
  promptContext, round1Analysis, provider, apiKey, model,
  imageBytes, mimeType, journalEntries = [], signal,
  callLLMImpl = callLLM, fetchImpl,
}) {
  if (!promptContext || typeof promptContext !== "object") {
    throw new Error("promptContext is required");
  }
  if (!round1Analysis || typeof round1Analysis !== "object") {
    throw new Error("round1Analysis is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  // 1. needs_ledger=true なら、呼出側が渡した復号済み仕訳から元帳テキストを
  //    クライアント側で構築する (E3-F PR-D-6-1b: 旧 POST /ai/ledger-context 撤去)。
  let ledgerText = "";
  const needsLedger = !!round1Analysis.needs_ledger
    && Array.isArray(round1Analysis.requested_accounts)
    && round1Analysis.requested_accounts.length > 0;
  if (needsLedger) {
    ledgerText = buildAccountsLedgerContext({
      accountNames: round1Analysis.requested_accounts,
      journalEntries,
      accountListText: promptContext.account_list_text,
    });
  }

  // 2. プロンプト構築
  const prompt = buildRound2Prompt({
    promptContext,
    needsLedger: needsLedger && ledgerText.length > 0,
    ledgerText,
  });

  // 3. LLM 呼出 (maxTokens は 2000、サーバ側と同じ)
  const llmRes = await callLLMImpl({
    provider, apiKey, model, imageBytes, mimeType, prompt,
    maxTokens: 2000, signal, fetchImpl: f,
  });

  // 4. account_code バリデーション
  const validCodes = parseAccountCodes(promptContext.account_list_text);
  const { suggestions, dropped } = validateSuggestions(
    llmRes.result?.suggestions, validCodes,
  );

  return {
    suggestions,
    dropped,
    usage: llmRes.usage,
    raw: llmRes.result,
    ledgerText,
  };
}
