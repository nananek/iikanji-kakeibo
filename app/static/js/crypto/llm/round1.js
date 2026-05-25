// Round 1: 画像 → 文書解析 (DocumentAnalysis) を JS 側で実行。
//
// サーバ側 ai_receipt.py analyze_and_suggest() の Round 1 部分と等価:
//   1. /api/v1/ai/prompt-context から round1_prompt + compliance_prompt +
//      custom_prompt + compliance_check_enabled を取得
//   2. prompt = round1_prompt + (compliance_prompt if enabled) +
//                custom_prompt + comment
//   3. callLLM で provider 別 LLM に画像 + prompt を送信
//   4. レスポンス JSON を DocumentAnalysis 形式に整形して返す
//
// 戻り値: { analysis, complianceResult, usage, raw }
//   analysis           : { date, description, amount, document_type, items,
//                          needs_ledger, requested_accounts }
//   complianceResult   : { status, warnings, details } or null
//   usage              : { input_tokens, output_tokens }
//   raw                : LLM の生 JSON (デバッグ用)

import { callLLM } from "./index.js";


/**
 * Round 1 プロンプトを構築する。サーバ側 ai_receipt.py の
 *   prompt = DOCUMENT_PROMPT
 *   if compliance_check: prompt += COMPLIANCE_CHECK_PROMPT
 *   if custom_prompt:    prompt += "\n\n## ユーザー定型情報\n" + custom_prompt
 *   if comment:          prompt += "\n\nユーザーからのコメント: " + comment
 * と等価。
 */
export function buildRound1Prompt({
  round1Prompt, complianceCheckEnabled = false, compliancePrompt = "",
  customPrompt = "", comment = "",
}) {
  if (typeof round1Prompt !== "string" || !round1Prompt) {
    throw new Error("round1Prompt is required");
  }
  let p = round1Prompt;
  if (complianceCheckEnabled && compliancePrompt) {
    p += compliancePrompt;
  }
  if (customPrompt) {
    p += `\n\n## ユーザー定型情報\n${customPrompt}`;
  }
  if (comment) {
    p += `\n\nユーザーからのコメント: ${comment}`;
  }
  return p;
}


/**
 * LLM の Round 1 JSON 戻り値を DocumentAnalysis dataclass 相当に整形する。
 * サーバ側 ai_receipt.py の DocumentAnalysis 構築と同じセマンティクス。
 */
function _toDocumentAnalysis(raw) {
  return {
    date: raw?.date ?? null,
    description: raw?.description ?? "",
    amount: Number.isFinite(Number(raw?.amount)) ? Number(raw.amount) : 0,
    document_type: raw?.document_type ?? "other",
    items: Array.isArray(raw?.items) ? raw.items : [],
    needs_ledger: raw?.needs_ledger === true,
    requested_accounts: Array.isArray(raw?.requested_accounts)
      ? raw.requested_accounts
      : [],
  };
}


/**
 * compliance チェック結果を整形 (status: pass/warn/fail のみ許容)。
 */
function _toComplianceResult(raw) {
  if (!raw || typeof raw !== "object") return null;
  const allowedStatus = ["pass", "warn", "fail"];
  let status = raw.status;
  if (!allowedStatus.includes(status)) status = "pass";
  return {
    status,
    warnings: Array.isArray(raw.warnings) ? raw.warnings : [],
    details: Array.isArray(raw.details) ? raw.details : [],
  };
}


/**
 * Round 1 を実行する。
 *
 * @param {Object} args
 * @param {Object} args.promptContext       /api/v1/ai/prompt-context レスポンス
 * @param {string} args.provider            "openai" | "anthropic" | "google"
 * @param {string} args.apiKey              MK で復号した api_key 平文
 * @param {string} args.model               UserAIConfig.model_name または
 *                                          promptContext.default_model_by_provider[provider]
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType
 * @param {string} [args.comment]
 * @param {AbortSignal} [args.signal]
 * @param {Function} [args.callLLMImpl=callLLM]   テスト DI
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<{analysis, complianceResult, usage, raw}>}
 */
export async function runRound1({
  promptContext, provider, apiKey, model, imageBytes, mimeType,
  comment = "", signal, callLLMImpl = callLLM, fetchImpl,
}) {
  if (!promptContext || typeof promptContext !== "object") {
    throw new Error("promptContext is required");
  }
  const prompt = buildRound1Prompt({
    round1Prompt: promptContext.round1_prompt,
    complianceCheckEnabled: !!promptContext.compliance_check_enabled,
    compliancePrompt: promptContext.compliance_prompt || "",
    customPrompt: promptContext.custom_prompt || "",
    comment,
  });
  // compliance_check で 1500、デフォルト 1000 (サーバ側 ai_receipt.py と同じ)
  const maxTokens = promptContext.compliance_check_enabled ? 1500 : 1000;
  const llmRes = await callLLMImpl({
    provider, apiKey, model, imageBytes, mimeType, prompt,
    maxTokens, signal, fetchImpl,
  });
  const raw = llmRes.result;
  const analysis = _toDocumentAnalysis(raw);
  const complianceResult = promptContext.compliance_check_enabled
    ? _toComplianceResult(raw?.compliance)
    : null;
  return {
    analysis,
    complianceResult,
    usage: llmRes.usage,
    raw,
  };
}
