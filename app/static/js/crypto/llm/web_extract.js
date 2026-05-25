// Web 明細抽出をクライアント完結で実行。
//
// 旧サーバ側 ai_receipt.parse_web_text と等価。クライアント
// 側で raw_text + payment_account_name → LLM → transactions[] の抽出を行い、
// サーバには平文テキストも API キーも一切送らない。
//
// フロー:
//   1. GET /api/v1/web-import/prompt-context で prompt_template +
//      default_model_by_provider を取得
//   2. __PAYMENT_ACCOUNT_NAME__ と __RAW_TEXT__ をユーザー入力で置換
//      (raw_text は 50000 字で切り詰め、サーバ側旧実装と同じ動作)
//   3. callLLMText で provider 別 text-only LLM 呼出
//   4. transactions[] をバリデーション (row_num 付与 + 整数化)
//
// 戻り値: { transactions, usage, raw }

import { callLLMText } from "./index.js";


const MAX_RAW_TEXT_LENGTH = 50_000;


/** prompt-context の prompt_template を実値で置換。raw_text は切り詰める。 */
export function buildWebExtractPrompt({
  promptTemplate, paymentAccountName, rawText,
}) {
  if (typeof promptTemplate !== "string" || !promptTemplate) {
    throw new Error("promptTemplate is required");
  }
  if (typeof paymentAccountName !== "string" || !paymentAccountName) {
    throw new Error("paymentAccountName is required");
  }
  if (typeof rawText !== "string") {
    throw new Error("rawText must be a string");
  }
  const truncated = rawText.slice(0, MAX_RAW_TEXT_LENGTH);
  // replaceAll で全プレースホルダを置換 (paymentAccountName 中に __RAW_TEXT__
  // 文字列を含んでも誤展開しない、PR #164 review Minor 1 対応)
  return promptTemplate
    .replaceAll("__PAYMENT_ACCOUNT_NAME__", paymentAccountName)
    .replaceAll("__RAW_TEXT__", truncated);
}


/** LLM 応答の transactions[] を整形 (サーバ側旧 parse_web_text と同形式)。 */
export function normalizeTransactions(raw) {
  const txs = Array.isArray(raw?.transactions) ? raw.transactions : [];
  return txs.map((tx, i) => ({
    row_num: i + 1,
    date: tx?.date ?? null,
    description: typeof tx?.description === "string" ? tx.description : "",
    deposit: Number.isFinite(Number(tx?.deposit)) ? Number(tx.deposit) : 0,
    withdrawal: Number.isFinite(Number(tx?.withdrawal))
      ? Number(tx.withdrawal) : 0,
  }));
}


/**
 * Web 明細抽出を実行する。
 *
 * @param {Object} args
 * @param {Object} args.promptContext            /api/v1/web-import/prompt-context レスポンス
 * @param {string} args.provider                 "openai" | "anthropic" | "google"
 * @param {string} args.apiKey                   MK で復号した api_key 平文
 * @param {string} args.model
 * @param {string} args.paymentAccountName
 * @param {string} args.rawText
 * @param {AbortSignal} [args.signal]
 * @param {Function} [args.callLLMTextImpl=callLLMText]  テスト DI
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<{transactions, usage, raw}>}
 */
export async function runWebExtract({
  promptContext, provider, apiKey, model,
  paymentAccountName, rawText,
  signal, callLLMTextImpl = callLLMText, fetchImpl,
}) {
  if (!promptContext || typeof promptContext !== "object") {
    throw new Error("promptContext is required");
  }
  if (typeof promptContext.prompt_template !== "string"
      || !promptContext.prompt_template) {
    throw new Error("promptContext.prompt_template is required");
  }
  const prompt = buildWebExtractPrompt({
    promptTemplate: promptContext.prompt_template,
    paymentAccountName,
    rawText,
  });
  const llmRes = await callLLMTextImpl({
    provider, apiKey, model, prompt,
    maxTokens: 16000, signal, fetchImpl,
  });
  return {
    transactions: normalizeTransactions(llmRes.result),
    usage: llmRes.usage,
    raw: llmRes.result,
  };
}
