// E2 PR-C-6c: AI 照合をクライアント完結で実行するオーケストレーター。
//
// サーバ側 find_ai_matches (本 PR で削除) と等価。csv_import の
// reconcileMode UI から呼ばれる。
//
// フロー:
//   1. GET /csv-import/ai-reconcile-context
//      → {prompt_template, batch_size, unmatched_csv, journal_candidates,
//         custom_prompt, default_model_by_provider}
//   2. GET /api/v1/ai-config + decrypt
//   3. unmatched_csv を batch_size 件ずつ分割
//   4. 各バッチで csv_text + journal_text を構築 → prompt 組立て
//      → callLLMText (maxTokens=2000) → matches を集約
//   5. {csv_index, entry_id, confidence, reason} の配列を返す
//      (confidence>=0.3 のみ、サーバ側旧 find_ai_matches と同基準)

import { callLLMText } from "./llm/index.js";


function b64decode(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function _fmtYen(n) {
  return Number(n || 0).toLocaleString("en-US");
}


async function _fetchPromptContext(fetchImpl) {
  const r = await fetchImpl("/csv-import/ai-reconcile-context", {
    credentials: "include",
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`reconcile-context fetch failed: ${e.error || `HTTP ${r.status}`}`);
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


export function formatCsvRows(rows) {
  return rows.map((r) =>
    `[${r.csv_index}] ${r.date || "?"} ${r.description || ""} ¥${_fmtYen(r.amount)}`
  ).join("\n");
}


export function formatJournalRows(rows) {
  return rows.map((r) =>
    `[ID:${r.entry_id}] ${r.date || "?"} ${r.description || ""} `
    + `¥${_fmtYen(r.amount)} (${r.category_name || ""})`
  ).join("\n");
}


export function buildReconcilePrompt({
  promptTemplate, csvRowsText, journalRowsText,
}) {
  if (typeof promptTemplate !== "string" || !promptTemplate) {
    throw new Error("promptTemplate is required");
  }
  return promptTemplate
    .replaceAll("__CSV_ROWS_TEXT__", csvRowsText)
    .replaceAll("__JOURNAL_ROWS_TEXT__", journalRowsText);
}


/** LLM 応答から有効な matches (confidence>=0.3) を抽出。 */
export function filterMatches(rawResult) {
  const out = [];
  if (!rawResult || typeof rawResult !== "object") return out;
  const matches = Array.isArray(rawResult.matches) ? rawResult.matches : [];
  for (const m of matches) {
    if (!m || m.entry_id === null || m.entry_id === undefined) continue;
    const conf = Number(m.confidence);
    if (!Number.isFinite(conf) || conf < 0.3) continue;
    out.push({
      csv_index: m.csv_index,
      entry_id: m.entry_id,
      confidence: conf,
      reason: m.reason || "",
    });
  }
  return out;
}


/**
 * AI 照合を実行 (バッチ処理 + 結果集約)。
 *
 * @param {Object} args
 * @param {Object} args.client                       SharedCryptoClient
 * @param {Function} [args.callLLMTextImpl=callLLMText]
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<Array>}                         [{csv_index, entry_id, confidence, reason}]
 */
export async function runReconcile({
  client, callLLMTextImpl = callLLMText, fetchImpl,
}) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  const [promptContext, cfg] = await Promise.all([
    _fetchPromptContext(f),
    _fetchAiConfig(f),
  ]);
  if (!cfg.is_e2ee || !cfg.api_key_blob || !cfg.api_key_iv) {
    throw new Error(
      "AI config が E2EE 形式ではありません。設定画面で移行してください。",
    );
  }

  const unmatched = promptContext.unmatched_csv || [];
  const candidates = promptContext.journal_candidates || [];
  if (unmatched.length === 0 || candidates.length === 0) return [];

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

  const batchSize = promptContext.batch_size || 30;
  const journalText = formatJournalRows(candidates);
  const allMatches = [];

  for (let start = 0; start < unmatched.length; start += batchSize) {
    const batch = unmatched.slice(start, start + batchSize);
    const csvText = formatCsvRows(batch);
    const prompt = buildReconcilePrompt({
      promptTemplate: promptContext.prompt_template,
      csvRowsText: csvText,
      journalRowsText: journalText,
    });
    const llmRes = await callLLMTextImpl({
      provider, apiKey, model, prompt,
      maxTokens: 2000, fetchImpl: f,
    });
    allMatches.push(...filterMatches(llmRes.result));
  }

  return allMatches;
}
