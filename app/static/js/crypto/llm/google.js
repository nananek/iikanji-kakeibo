// Google Gemini generateContent API の fetch ラッパー (E2 PR-C-1)。
//
// サーバ側 _call_google と等価の挙動。
// 認証: クエリパラメータ ?key=<apiKey> (Bearer ではない)。

import { extractJson, bytesToBase64 } from "./parse.js";


const GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models";


/**
 * Gemini generateContent を呼んで {result, usage} を返す。
 *
 * @param {Object} args
 * @param {string} args.apiKey
 * @param {string} args.model        "gemini-1.5-flash" 等
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType     "image/jpeg" 等
 * @param {string} args.prompt
 * @param {number} [args.maxTokens=2000]
 * @param {AbortSignal} [args.signal]
 * @param {Function}    [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{result: any, usage: {input_tokens, output_tokens}}>}
 */
export async function callGoogle({
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
  const url = `${GOOGLE_URL}/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const body = {
    contents: [{
      parts: [
        { text: prompt },
        { inline_data: { mime_type: mimeType, data: b64 } },
      ],
    }],
    generationConfig: {
      responseMimeType: "application/json",
      maxOutputTokens: maxTokens,
    },
  };
  const r = await f(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(
      `Google API error: HTTP ${r.status} ${text.slice(0, 200)}`,
    );
  }
  const data = await r.json();
  const content = data?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (typeof content !== "string") {
    throw new Error("Google response missing content");
  }
  return {
    result: extractJson(content),
    usage: {
      input_tokens: data?.usageMetadata?.promptTokenCount ?? null,
      output_tokens: data?.usageMetadata?.candidatesTokenCount ?? null,
    },
  };
}
