// 取込確認画面の科目推定 AI をクライアント完結で実行する
// オーケストレーター。
//
// 旧サーバ側 suggest_categories_by_ai と等価。alpine
// `importConfirm.aiSuggestCategories` から呼ばれる。
//
// フロー:
//   1. GET /api/v1/suggest-categories/prompt-context?payment_account_code=
//      → {prompt_template, payment_account_name, ledger_context,
//         account_list, account_map, default_model_by_provider, custom_prompt}
//   2. GET /api/v1/ai-config + decrypt
//   3. クライアント側で rows_text を構築、プロンプト組立て
//   4. callLLMText → {results: [{index, account_code}]}
//   5. account_map で account_code → account_name 解決
//   6. {description: {account_code, account_name}} のマップを返す
//      (サーバ側旧 suggest_categories_by_ai と同形)

import { callLLMText } from "./llm/index.js";
import { b64decode } from "./b64.js";


function _fmtYen(n) {
  return Number(n || 0).toLocaleString("en-US");
}


async function _fetchPromptContext(fetchImpl, paymentAccountCode) {
  const url = "/api/v1/suggest-categories/prompt-context?payment_account_code="
    + encodeURIComponent(paymentAccountCode);
  const r = await fetchImpl(url, { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`prompt-context fetch failed: ${e.error || `HTTP ${r.status}`}`);
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


/** rows[] を rows_text (LLM 入力用の番号付き文字列) に整形。 */
export function buildRowsText(rows) {
  if (!Array.isArray(rows)) throw new Error("rows must be an array");
  return rows.map((row, i) => {
    const desc = row?.description ?? "";
    const dep = _fmtYen(row?.deposit);
    const wd = _fmtYen(row?.withdrawal);
    return `${i}. ${desc} (入金: ¥${dep}, 出金: ¥${wd})`;
  }).join("\n");
}


export function buildSuggestCategoriesPrompt({
  promptTemplate, paymentAccountName, ledgerContext, accountList, rowsText,
}) {
  if (typeof promptTemplate !== "string" || !promptTemplate) {
    throw new Error("promptTemplate is required");
  }
  return promptTemplate
    .replaceAll("__PAYMENT_ACCOUNT_NAME__", String(paymentAccountName ?? ""))
    .replaceAll("__LEDGER_CONTEXT__", String(ledgerContext ?? ""))
    .replaceAll("__ACCOUNT_LIST__", String(accountList ?? ""))
    .replaceAll("__ROWS_TEXT__", rowsText);
}


/**
 * LLM 出力 {results: [{index, account_code}]} を旧サーバ形式に整形。
 * @returns {Object} {description: {account_code, account_name}}
 */
export function normalizeSuggestions(raw, rows, accountMap) {
  const out = {};
  const results = Array.isArray(raw?.results) ? raw.results : [];
  for (const item of results) {
    const idx = item?.index;
    const acode = item?.account_code;
    if (idx === null || idx === undefined || acode === null || acode === undefined) {
      continue;
    }
    if (idx < 0 || idx >= rows.length) continue;
    const desc = rows[idx]?.description;
    if (!desc || desc in out) continue;
    const code = String(acode);
    const name = accountMap?.[code];
    if (!name) continue; // 無効な code はスキップ (サーバ側 Account.query 同等)
    out[desc] = { account_code: code, account_name: name };
  }
  return out;
}


/**
 * 科目推定 AI を実行。
 *
 * @param {Object} args
 * @param {string} args.paymentAccountCode
 * @param {Array} args.rows                          [{description, deposit, withdrawal}, ...]
 * @param {Object} args.client                       SharedCryptoClient
 * @param {Function} [args.callLLMTextImpl=callLLMText]
 * @param {Function} [args.fetchImpl]
 * @returns {Promise<Object>}                        {description: {account_code, account_name}}
 */
export async function runSuggestCategories({
  paymentAccountCode, rows, client,
  callLLMTextImpl = callLLMText, fetchImpl,
}) {
  if (!paymentAccountCode) {
    throw new Error("paymentAccountCode is required");
  }
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error("rows is required");
  }
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  const [promptContext, cfg] = await Promise.all([
    _fetchPromptContext(f, paymentAccountCode),
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

  const rowsText = buildRowsText(rows);
  const prompt = buildSuggestCategoriesPrompt({
    promptTemplate: promptContext.prompt_template,
    paymentAccountName: promptContext.payment_account_name,
    ledgerContext: promptContext.ledger_context,
    accountList: promptContext.account_list,
    rowsText,
  });

  const llmRes = await callLLMTextImpl({
    provider, apiKey, model, prompt,
    maxTokens: 4000, fetchImpl: f,
  });
  return normalizeSuggestions(llmRes.result, rows, promptContext.account_map);
}
