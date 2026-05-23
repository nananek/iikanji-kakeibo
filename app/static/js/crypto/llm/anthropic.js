// Anthropic Claude Messages API の fetch ラッパー (E2 PR-C-1)。
//
// サーバ側 _call_anthropic と等価の挙動。
// 認証: x-api-key ヘッダ (Bearer ではない)。
// anthropic-version ヘッダ必須。

import { extractJson, bytesToBase64 } from "./parse.js";


const ANTHROPIC_URL = "https://api.anthropic.com/v1/messages";
const ANTHROPIC_VERSION = "2023-06-01";


/**
 * Anthropic Messages API を呼んで {result, usage} を返す。
 *
 * @param {Object} args
 * @param {string} args.apiKey
 * @param {string} args.model       "claude-3-5-sonnet-20241022" 等
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType    "image/jpeg" 等
 * @param {string} args.prompt
 * @param {number} [args.maxTokens=2000]
 * @param {AbortSignal} [args.signal]
 * @param {Function}    [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{result: any, usage: {input_tokens, output_tokens}}>}
 */
export async function callAnthropic({
  apiKey, model, imageBytes, mimeType, prompt,
  maxTokens = 2000, signal, fetchImpl,
}) {
  if (!apiKey || typeof apiKey !== "string") {
    throw new Error("apiKey is required");
  }
  if (!model || typeof model !== "string") {
    throw new Error("model is required");
  }
  if (!(imageBytes instanceof Uint8Array)) {
    throw new Error("imageBytes must be Uint8Array");
  }
  const f = fetchImpl ?? globalThis.fetch;
  const b64 = bytesToBase64(imageBytes);
  const body = {
    model,
    max_tokens: maxTokens,
    messages: [{
      role: "user",
      content: [
        {
          type: "image",
          source: { type: "base64", media_type: mimeType, data: b64 },
        },
        { type: "text", text: prompt },
      ],
    }],
  };
  const r = await f(ANTHROPIC_URL, {
    method: "POST",
    headers: {
      "x-api-key": apiKey,
      "anthropic-version": ANTHROPIC_VERSION,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(
      `Anthropic API error: HTTP ${r.status} ${text.slice(0, 200)}`,
    );
  }
  const data = await r.json();
  const content = data?.content?.[0]?.text;
  if (typeof content !== "string") {
    throw new Error("Anthropic response missing content");
  }
  return {
    result: extractJson(content),
    usage: {
      input_tokens: data?.usage?.input_tokens ?? null,
      output_tokens: data?.usage?.output_tokens ?? null,
    },
  };
}
