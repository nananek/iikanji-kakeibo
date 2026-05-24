// E2 PR-C-6d: CSV mapping 画面の AI 列推定をクライアント完結で実行する
// オーケストレーター。
//
// サーバ側 csv_import.detect_columns_by_ai (本 PR で削除) と等価。
// mapping.html UI から呼ばれ、推定結果を select に反映する。
//
// フロー:
//   1. POST /csv-import/api/columns-detect-context (headers + sample_rows)
//      → {prompt_template, headers_text, sample_text, sample_count,
//         num_cols, custom_prompt, default_model_by_provider}
//   2. GET /api/v1/ai-config + decrypt → api_key 平文
//   3. プレースホルダ (__HEADERS_TEXT__ / __SAMPLE_TEXT__ / __SAMPLE_COUNT__)
//      を置換 → callLLMText (maxTokens=500)
//   4. 結果を {date_col, desc_col, date_format, deposit_col, withdrawal_col}
//      に整形 + バリデーション (range check)

import { callLLMText } from "./llm/index.js";


function b64decode(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function _csrf() {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


async function _fetchPromptContext(fetchImpl, headers, sampleRows) {
  const r = await fetchImpl("/csv-import/api/columns-detect-context", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": _csrf(),
    },
    body: JSON.stringify({ headers, sample_rows: sampleRows }),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`columns-detect-context fetch failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}

async function _fetchAiConfig(fetchImpl) {
  const r = await fetchImpl("/api/v1/ai-config", { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`ai-config fetch failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}


export function buildDetectPrompt({
  promptTemplate, headersText, sampleText, sampleCount,
}) {
  if (typeof promptTemplate !== "string" || !promptTemplate) {
    throw new Error("promptTemplate is required");
  }
  return promptTemplate
    .replaceAll("__HEADERS_TEXT__", String(headersText ?? ""))
    .replaceAll("__SAMPLE_TEXT__", String(sampleText ?? ""))
    .replaceAll("__SAMPLE_COUNT__", String(sampleCount ?? 0));
}


/** LLM 応答 → mapping dict 整形 + バリデーション。サーバ側
 * validate_ai_column_mapping と等価ロジック。 */
export function validateMapping(result, numCols) {
  if (!result || typeof result !== "object") return null;
  const n = Number.isFinite(numCols) ? Math.max(numCols, 0) : 0;
  const dateCol = Number(result.date_col);
  const descCol = Number(result.desc_col);
  if (!Number.isFinite(dateCol) || !Number.isFinite(descCol)) return null;
  if (dateCol < 0 || dateCol >= n) return null;
  if (descCol < 0 || descCol >= n) return null;
  const mapping = {
    date_col: dateCol,
    desc_col: descCol,
    date_format: typeof result.date_format === "string"
      ? result.date_format : "%Y/%m/%d",
    deposit_col: null,
    withdrawal_col: null,
  };
  const dep = result.deposit_col;
  const wd = result.withdrawal_col;
  if (dep !== null && dep !== undefined) {
    const d = Number(dep);
    if (Number.isFinite(d) && d >= 0 && d < n) mapping.deposit_col = d;
  }
  if (wd !== null && wd !== undefined) {
    const w = Number(wd);
    if (Number.isFinite(w) && w >= 0 && w < n) mapping.withdrawal_col = w;
  }
  return mapping;
}


/**
 * AI 列推定をクライアント完結で実行する。
 *
 * @param {Object} args
 * @param {Array<string>} args.headers
 * @param {Array<Array<string>>} args.sampleRows
 * @param {Object} args.client                       SharedCryptoClient
 * @param {Function} [args.callLLMTextImpl=callLLMText]
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<Object|null>}  mapping dict or null (失敗時)
 */
export async function runColumnsDetect({
  headers, sampleRows, client,
  callLLMTextImpl = callLLMText, fetchImpl,
}) {
  if (!Array.isArray(headers) || headers.length === 0) {
    throw new Error("headers is required");
  }
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  const [promptContext, cfg] = await Promise.all([
    _fetchPromptContext(f, headers, sampleRows || []),
    _fetchAiConfig(f),
  ]);
  if (!cfg.is_e2ee || !cfg.api_key_blob || !cfg.api_key_iv) {
    throw new Error(
      "AI config が E2EE 形式ではありません。設定画面で移行してください。",
    );
  }

  const blob = b64decode(cfg.api_key_blob);
  const iv = b64decode(cfg.api_key_iv);
  const decryptResult = await client.decrypt(blob, iv);
  const plaintextBytes = decryptResult.plaintext;
  const apiKey = new TextDecoder().decode(plaintextBytes);
  try { plaintextBytes.fill(0); } catch (_e) { /* ignore */ }

  const provider = cfg.provider;
  const model = cfg.model_name
    || promptContext.default_model_by_provider?.[provider]
    || "";
  if (!model) {
    throw new Error(`unsupported provider for client-side analysis: ${provider}`);
  }

  const prompt = buildDetectPrompt({
    promptTemplate: promptContext.prompt_template,
    headersText: promptContext.headers_text,
    sampleText: promptContext.sample_text,
    sampleCount: promptContext.sample_count,
  });

  const llmRes = await callLLMTextImpl({
    provider, apiKey, model, prompt,
    maxTokens: 500, fetchImpl: f,
  });
  return validateMapping(llmRes.result, promptContext.num_cols);
}
