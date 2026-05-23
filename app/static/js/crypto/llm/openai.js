// OpenAI Chat Completions の fetch ラッパー (E2 PR-C-1)。
//
// サーバ側 _call_openai と等価の挙動を実装。クライアント (ブラウザ) が
// 直接 OpenAI API を呼び、サーバには平文画像 / プロンプトを送らない。
//
// セキュリティ:
// - API キーは MK で復号した後、メモリ上で短時間保持して直接 API に送る
// - サーバには到達しない (本ファイルは全て client-side で動く想定)
// - timeout は AbortSignal で外部から制御

import { extractJson, bytesToBase64 } from "./parse.js";


const OPENAI_URL = "https://api.openai.com/v1/chat/completions";


/**
 * OpenAI Chat Completions API を呼んで {result, usage} を返す。
 *
 * @param {Object} args
 * @param {string} args.apiKey
 * @param {string} args.model      "gpt-4o" 等
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType   "image/jpeg" 等
 * @param {string} args.prompt
 * @param {number} [args.maxTokens=2000]
 * @param {AbortSignal} [args.signal]
 * @param {Function}    [args.fetchImpl=globalThis.fetch]  テスト用
 * @returns {Promise<{result: any, usage: {input_tokens, output_tokens}}>}
 */
export async function callOpenAI({
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
    messages: [{
      role: "user",
      content: [
        { type: "text", text: prompt },
        {
          type: "image_url",
          image_url: { url: `data:${mimeType};base64,${b64}` },
        },
      ],
    }],
    max_tokens: maxTokens,
  };
  const r = await f(OPENAI_URL, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(
      `OpenAI API error: HTTP ${r.status} ${text.slice(0, 200)}`,
    );
  }
  const data = await r.json();
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content !== "string") {
    throw new Error("OpenAI response missing content");
  }
  return {
    result: extractJson(content),
    usage: {
      input_tokens: data?.usage?.prompt_tokens ?? null,
      output_tokens: data?.usage?.completion_tokens ?? null,
    },
  };
}
