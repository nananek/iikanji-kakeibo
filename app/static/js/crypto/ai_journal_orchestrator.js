// AI 証憑解析のクライアント側オーケストレーター。
//
// 既存 ai_analyze.js が「単発の analyze (LLM 1 回)」を提供していたのに対し、
// 本モジュールはサーバ側 ai_receipt.analyze_and_suggest() と等価な
// Round 1 → 元帳取得 → Round 2 の 2 段階フローをクライアント完結で実行する。
//
// 全体フロー:
//   1. POST /api/v1/ai/uploads (画像 + comment 送信)
//      → draft_id 取得 (サーバは LLM 呼ばない、AIDraft.status="pending")
//   2. GET /api/v1/ai-config (provider/model/blob/iv 取得)
//      → SharedCryptoClient.decrypt で api_key 復号
//   3. GET /api/v1/ai/prompt-context (Round 1+2 プロンプト材料一括取得)
//   4. runRound1 (画像 → DocumentAnalysis + compliance)
//   5. needs_ledger=true なら ledger-context fetch → runRound2 で仕訳案生成
//   6. PATCH /api/v1/ai/drafts/<id>/suggestions (結果保存 + AIUsageLog 記録)
//
// セキュリティ:
//   - api_key 平文はメモリ上 string、復号 raw bytes は直後にゼロ埋め
//   - サーバには画像のみ送信 (LLM プロンプトと API キーはサーバを通らない)
//   - usage は Round 1 + Round 2 の合算を AIUsageLog に記録

import { runRound1 } from "./llm/round1.js";
import { runRound2 } from "./llm/round2.js";
import { b64decode } from "./b64.js";


function _csrf() {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


/** POST /api/v1/ai/uploads (multipart) */
async function _uploadImage(fetchImpl, file, comment) {
  const form = new FormData();
  form.append("image", file);
  if (comment) form.append("comment", comment);
  const r = await fetchImpl("/api/v1/ai/uploads", {
    method: "POST", credentials: "include", body: form,
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`upload failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}

/** GET /api/v1/ai-config */
async function _fetchAiConfig(fetchImpl) {
  const r = await fetchImpl("/api/v1/ai-config", { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`ai-config fetch failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}

/** GET /api/v1/ai/prompt-context */
async function _fetchPromptContext(fetchImpl) {
  const r = await fetchImpl("/api/v1/ai/prompt-context", {
    credentials: "include",
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`prompt-context fetch failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}

/** PATCH /api/v1/ai/drafts/<id>/suggestions */
async function _saveSuggestions(fetchImpl, draftId, body) {
  const r = await fetchImpl(`/api/v1/ai/drafts/${draftId}/suggestions`, {
    method: "PATCH",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": _csrf(),
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`save suggestions failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}


/**
 * AI 証憑解析の全体フロー (Round 1 + Round 2) をクライアント側で実行する。
 *
 * @param {Object} args
 * @param {File|Blob} args.file
 * @param {string} [args.comment]
 * @param {Object} args.client                   SharedCryptoClient (decrypt 必須)
 * @param {Function} [args.runRound1Impl=runRound1]   テスト DI
 * @param {Function} [args.runRound2Impl=runRound2]
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{draft_id, suggestions, analysis, complianceResult, provider, usage}>}
 */
export async function analyzeReceiptFull({
  file, comment = "", client,
  runRound1Impl = runRound1, runRound2Impl = runRound2, fetchImpl,
}) {
  if (!file) throw new Error("file is required");
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  // 1. 画像アップロード
  const { draft_id } = await _uploadImage(f, file, comment);

  // 2. AI 設定取得 + MK 復号
  const cfg = await _fetchAiConfig(f);
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
  try { plaintextBytes.fill(0); } catch (_e) { /* detached / immutable */ }

  // 3. プロンプトコンテキスト取得
  const promptContext = await _fetchPromptContext(f);

  // 4. Round 1 (画像 → DocumentAnalysis)
  const provider = cfg.provider;
  const model = cfg.model_name
    || promptContext.default_model_by_provider?.[provider]
    || "";
  if (!model) {
    throw new Error(`unsupported provider for client-side analysis: ${provider}`);
  }
  const imageBytes = new Uint8Array(await file.arrayBuffer());
  const r1 = await runRound1Impl({
    promptContext, provider, apiKey, model, imageBytes,
    mimeType: file.type || "image/jpeg",
    comment, fetchImpl: f,
  });

  // 5. Round 2 (Round 1 結果 + 元帳 → 仕訳案)
  const r2 = await runRound2Impl({
    promptContext, round1Analysis: r1.analysis,
    provider, apiKey, model, imageBytes,
    mimeType: file.type || "image/jpeg",
    fetchImpl: f,
  });

  // 6. 結果保存 (AIUsageLog 記録)
  // Round 1 + Round 2 の usage を合算してサーバに送る
  const usage = {
    input_tokens: (r1.usage?.input_tokens || 0) + (r2.usage?.input_tokens || 0),
    output_tokens: (r1.usage?.output_tokens || 0) + (r2.usage?.output_tokens || 0),
  };
  await _saveSuggestions(f, draft_id, {
    suggestions: r2.suggestions,
    usage,
    provider,
    model,
  });

  return {
    draft_id,
    suggestions: r2.suggestions,
    analysis: r1.analysis,
    complianceResult: r1.complianceResult,
    provider,
    model,
    usage,
    dropped: r2.dropped,
  };
}
