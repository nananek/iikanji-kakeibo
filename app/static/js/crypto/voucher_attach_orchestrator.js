// 証憑添付時の AI 解析 (コンプライアンス + 仕訳整合性) を
// クライアント完結で実行するオーケストレーター。
//
// 旧サーバ側 analyze_voucher_for_attachment と等価。
// ledger.html UI が voucher attach 完了後にこれを呼ぶ。
//
// フロー:
//   1. GET /api/v1/voucher-attach/prompt-context
//   2. GET /api/v1/ai-config + decrypt → api_key 平文
//   3. prompt template の __JOURNAL_DATE__ / __JOURNAL_AMOUNT__ /
//      __JOURNAL_DESCRIPTION__ を置換
//   4. callLLM (画像) → 結果整形 → {compliance, consistency} 返却
//
// 戻り値はサーバ側旧 analyze_voucher_for_attachment と同形:
//   { compliance: {status, warnings, details} | null,
//     consistency: {status, date_match, amount_match, description_match, warnings} }

import { callLLM } from "./llm/index.js";
import { b64decode } from "./b64.js";



async function _fetchPromptContext(fetchImpl) {
  const r = await fetchImpl("/api/v1/voucher-attach/prompt-context", {
    credentials: "include",
  });
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


export function buildVoucherAttachPrompt({
  promptTemplate, journalDate, journalAmount, journalDescription,
}) {
  if (typeof promptTemplate !== "string" || !promptTemplate) {
    throw new Error("promptTemplate is required");
  }
  return promptTemplate
    .replaceAll("__JOURNAL_DATE__", String(journalDate ?? ""))
    .replaceAll("__JOURNAL_AMOUNT__", String(journalAmount ?? 0))
    .replaceAll("__JOURNAL_DESCRIPTION__", String(journalDescription ?? ""));
}


function _toCompliance(raw) {
  if (!raw || typeof raw !== "object") return null;
  const allowed = ["pass", "warn", "fail"];
  let status = raw.status;
  if (!allowed.includes(status)) status = "pass";
  return {
    status,
    warnings: Array.isArray(raw.warnings) ? raw.warnings : [],
    details: Array.isArray(raw.details) ? raw.details : [],
  };
}


function _toConsistency(raw) {
  if (!raw || typeof raw !== "object") {
    return {
      status: "warn",
      date_match: false, amount_match: false, description_match: false,
      warnings: ["AI が consistency を返しませんでした"],
    };
  }
  const allowed = ["pass", "warn", "fail"];
  let status = raw.status;
  if (!allowed.includes(status)) status = "warn";
  return {
    status,
    date_match: !!raw.date_match,
    amount_match: !!raw.amount_match,
    description_match: !!raw.description_match,
    warnings: Array.isArray(raw.warnings) ? raw.warnings : [],
  };
}


/**
 * 証憑画像 + 既存仕訳メタ → クライアント側 LLM で {compliance, consistency} を返す。
 *
 * @param {Object} args
 * @param {Uint8Array} args.imageBytes
 * @param {string} args.mimeType
 * @param {string} args.journalDate
 * @param {number} args.journalAmount
 * @param {string} args.journalDescription
 * @param {Object} args.client                       SharedCryptoClient
 * @param {Function} [args.callLLMImpl=callLLM]      テスト DI
 * @param {Function} [args.fetchImpl]
 */
export async function runVoucherAttachAnalysis({
  imageBytes, mimeType,
  journalDate, journalAmount, journalDescription,
  client, callLLMImpl = callLLM, fetchImpl,
}) {
  if (!(imageBytes instanceof Uint8Array)) {
    throw new Error("imageBytes must be Uint8Array");
  }
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

  const prompt = buildVoucherAttachPrompt({
    promptTemplate: promptContext.prompt_template,
    journalDate, journalAmount, journalDescription,
  });

  const llmRes = await callLLMImpl({
    provider, apiKey, model, imageBytes, mimeType, prompt,
    maxTokens: 1500, fetchImpl: f,
  });
  const raw = llmRes.result;

  return {
    compliance: promptContext.compliance_check_enabled
      ? _toCompliance(raw?.compliance)
      : null,
    consistency: _toConsistency(raw?.consistency),
    usage: llmRes.usage,
    raw,
  };
}
