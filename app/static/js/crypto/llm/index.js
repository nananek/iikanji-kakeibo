// クライアント側 LLM 呼出の統一インターフェイス。
//
// 設計書 §11.3 / §11.6: v5.0 では AI 呼出をクライアント側で完結させる。
// サーバ側 _PROVIDER_HANDLERS と等価の振る舞いを JS で実装し、ai-journal
// UI から本モジュールを通じて LLM を呼ぶ。
//
// 対応 provider:
//   - openai
//   - anthropic
//   - google
//
// llama_cpp は v5.0 で廃止 (E2EE 化に伴いサーバ側鍵管理が前提だった構造を
// 廃止、Self-hosted LLM は将来 E2EE 化された別経路で再導入する場合は別 PR)。

import { callOpenAI, callOpenAIText } from "./openai.js";
import { callAnthropic, callAnthropicText } from "./anthropic.js";
import { callGoogle, callGoogleText } from "./google.js";


/**
 * provider → handler (image + text)。テストや mock 注入用に export。
 */
export const LLM_HANDLERS = Object.freeze({
  openai: callOpenAI,
  anthropic: callAnthropic,
  google: callGoogle,
});


/** provider → text-only handler (Web 明細抽出用)。 */
export const LLM_TEXT_HANDLERS = Object.freeze({
  openai: callOpenAIText,
  anthropic: callAnthropicText,
  google: callGoogleText,
});


/**
 * provider 別に LLM API を呼んで {result, usage} を返す。
 *
 * @param {Object} args
 * @param {string} args.provider  "openai" | "anthropic" | "google"
 * @param {string} args.apiKey
 * @param {string} args.model
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType
 * @param {string} args.prompt
 * @param {number} [args.maxTokens]
 * @param {AbortSignal} [args.signal]
 * @param {Function}    [args.fetchImpl]
 */
export async function callLLM(args) {
  const handler = LLM_HANDLERS[args.provider];
  if (!handler) {
    throw new Error(
      `unknown provider: ${args.provider} (supported: ${Object.keys(LLM_HANDLERS).join(", ")})`,
    );
  }
  return handler(args);
}


/** provider 別に text-only LLM API を呼んで {result, usage} を返す。 */
export async function callLLMText(args) {
  const handler = LLM_TEXT_HANDLERS[args.provider];
  if (!handler) {
    throw new Error(
      `unknown provider: ${args.provider} (supported: ${Object.keys(LLM_TEXT_HANDLERS).join(", ")})`,
    );
  }
  return handler(args);
}
