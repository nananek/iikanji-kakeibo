// クライアント側 AI 証憑解析オーケストレーター。
//
// 全体フロー:
//   1. 画像を /api/v1/ai/uploads にアップロード (LLM 呼出なし)
//      → draft_id を取得
//   2. /api/v1/ai-config から暗号化済 api_key (blob + iv) を取得
//   3. MK (SharedCryptoClient) で blob を復号 → 平文 api_key を得る
//   4. callLLM (provider 振り分け) で LLM API を直接呼ぶ
//   5. PATCH /api/v1/ai/drafts/<id>/suggestions で結果をサーバに保存
//
// セキュリティ:
//   - 平文 api_key はメモリ上で string として短時間保持 (JS string は GC 後に
//     即時クリアされない制約は WebCrypto API レベルの制約として受容)
//   - 平文 Uint8Array (decrypt 結果) は callLLM 直後にゼロ埋め
//   - 平文画像 / プロンプト / 解析結果は v5.0 では暗号化対象 (E3-)、本 PR
//     段階ではまだ平文でサーバに送る (移行期間として許容)
//
// 設計書 §11.3 / §11.6 参照。

import { callLLM } from "./llm/index.js";


function b64decode(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function csrfToken() {
  const meta = (typeof document !== "undefined")
    ? document.querySelector('meta[name="csrf-token"]')
    : null;
  return meta ? meta.getAttribute("content") : "";
}


/**
 * AI 設定取得。/api/v1/ai-config の戻り値をそのまま。
 */
async function _fetchAiConfig(fetchImpl) {
  const r = await fetchImpl("/api/v1/ai-config", { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`ai-config fetch failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}


/**
 * 画像をアップロード。サーバは LLM 呼出を行わず draft_id を返すのみ。
 */
async function _uploadImage(fetchImpl, file, comment) {
  const form = new FormData();
  form.append("image", file);
  if (comment) form.append("comment", comment);
  const r = await fetchImpl("/api/v1/ai/uploads", {
    method: "POST",
    credentials: "include",
    body: form,
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`upload failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}


/**
 * suggestions を保存。
 */
async function _saveSuggestions(fetchImpl, draftId, body) {
  const r = await fetchImpl(`/api/v1/ai/drafts/${draftId}/suggestions`, {
    method: "PATCH",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
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
 * クライアント側 AI 解析フロー (5 ステップ) を一気通貫で実行する。
 *
 * @param {Object} args
 * @param {File|Blob} args.file       画像 (input[type=file] から取得)
 * @param {string} [args.comment]
 * @param {string} args.prompt        LLM に渡すプロンプト本文
 * @param {Object} args.client        SharedCryptoClient (encrypt/decrypt が使える)
 * @param {Function} [args.callLLMImpl=callLLM]   テスト用 LLM 呼出関数の DI
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{draft_id, suggestions, usage, provider}>}
 */
export async function analyzeReceiptClientSide({
  file, comment = "", prompt, client,
  callLLMImpl = callLLM, fetchImpl,
}) {
  if (!file) throw new Error("file is required");
  if (!prompt || typeof prompt !== "string") {
    throw new Error("prompt is required");
  }
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  // 1. 画像アップロード → draft_id
  const { draft_id } = await _uploadImage(f, file, comment);

  // 2. AI 設定取得 (E2EE 形式 blob + iv)
  const cfg = await _fetchAiConfig(f);
  if (!cfg.is_e2ee || !cfg.api_key_blob || !cfg.api_key_iv) {
    throw new Error(
      "AI config が E2EE 形式ではありません。設定画面で移行してください。",
    );
  }

  // 3. MK で api_key 復号
  const blob = b64decode(cfg.api_key_blob);
  const iv = b64decode(cfg.api_key_iv);
  const decryptResult = await client.decrypt(blob, iv);
  // decrypt の戻り値は { plaintext: Uint8Array }
  const plaintextBytes = decryptResult.plaintext;
  const apiKey = new TextDecoder().decode(plaintextBytes);
  // 復号後すぐに raw bytes を消す (api_key string は GC 任せ)
  try { plaintextBytes.fill(0); } catch (_e) { /* detached / immutable */ }

  // 4. LLM 呼出
  const usedModel = cfg.model_name || _defaultModelFor(cfg.provider);
  const imageBytes = new Uint8Array(await file.arrayBuffer());
  const llmRes = await callLLMImpl({
    provider: cfg.provider,
    apiKey,
    model: usedModel,
    imageBytes,
    mimeType: file.type || "image/jpeg",
    prompt,
    fetchImpl: f,
  });

  // 5. suggestions をサーバに保存。provider/model も送ることで AIUsageLog
  // (サーバ側 ai_receipt.py と等価の監査トレイル) を記録可能にする。
  // LLM は単一 JSON or 配列を返す可能性があるので配列化。
  const suggestions = Array.isArray(llmRes.result)
    ? llmRes.result
    : [llmRes.result];
  const saved = await _saveSuggestions(f, draft_id, {
    suggestions,
    usage: llmRes.usage,
    provider: cfg.provider,
    model: usedModel,
  });

  return {
    draft_id,
    suggestions,
    usage: llmRes.usage,
    provider: cfg.provider,
    saved,
  };
}


/**
 * provider 別のデフォルトモデル (設定で model_name 未指定の場合)。
 * テストからも参照できるよう export する。
 */
export function defaultModelFor(provider) {
  switch (provider) {
    case "openai":    return "gpt-4o-mini";
    case "anthropic": return "claude-3-5-sonnet-20241022";
    case "google":    return "gemini-1.5-flash";
    default:          return "";
  }
}

// 後方互換: 既存呼出名
const _defaultModelFor = defaultModelFor;
