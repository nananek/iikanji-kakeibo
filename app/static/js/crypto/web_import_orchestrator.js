// Web 明細抽出のクライアント完結オーケストレーター。
//
// サーバ側 ai_receipt.parse_web_text + /web-import/ POST と等価のフローを
// クライアント完結で実行する。
//
// 全体フロー:
//   1. GET /api/v1/web-import/prompt-context (prompt template + デフォルトモデル)
//   2. GET /api/v1/ai-config (provider/model/blob/iv)
//   3. SharedCryptoClient.decrypt で MK 復号 → api_key 平文
//   4. runWebExtract で text-LLM 呼出し → transactions[]
//   5. POST /web-import/ (JSON, {parsed_transactions, payment_account_code})
//      → session 保存 + redirect_url 受領
//
// セキュリティ:
//   - api_key 平文はメモリ上 string、復号 raw bytes は直後にゼロ埋め
//   - サーバには parsed_transactions (LLM が抽出した明細) のみ送信
//     (raw_text と API キーはサーバを通らない)

import { runWebExtract } from "./llm/web_extract.js";
import { b64decode } from "./b64.js";


function _csrf() {
  if (typeof document === "undefined") return "";
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


async function _fetchPromptContext(fetchImpl) {
  const r = await fetchImpl("/api/v1/web-import/prompt-context", {
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

async function _saveParsedToSession(fetchImpl, body) {
  const r = await fetchImpl("/web-import/", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": _csrf(),
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`save parsed failed: ${e.error || `HTTP ${r.status}`}`);
  }
  return r.json();
}


/**
 * Web 明細抽出 → session 保存までを一気通貫で実行。
 *
 * @param {Object} args
 * @param {string} args.rawText
 * @param {string} args.paymentAccountCode
 * @param {string} args.paymentAccountName
 * @param {Object} args.client                      SharedCryptoClient (decrypt 必須)
 * @param {Function} [args.runWebExtractImpl=runWebExtract]  テスト DI
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @returns {Promise<{transactions, usage, redirect_url}>}
 */
export async function extractAndSaveWebText({
  rawText, paymentAccountCode, paymentAccountName, client,
  runWebExtractImpl = runWebExtract, fetchImpl,
}) {
  if (typeof rawText !== "string" || !rawText) {
    throw new Error("rawText is required");
  }
  if (!paymentAccountCode) {
    throw new Error("paymentAccountCode is required");
  }
  if (!paymentAccountName) {
    throw new Error("paymentAccountName is required");
  }
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const f = fetchImpl ?? globalThis.fetch;

  // 1. prompt context + 2. ai-config をパラレル取得
  const [promptContext, cfg] = await Promise.all([
    _fetchPromptContext(f),
    _fetchAiConfig(f),
  ]);
  if (!cfg.is_e2ee || !cfg.api_key_blob || !cfg.api_key_iv) {
    throw new Error(
      "AI config が E2EE 形式ではありません。設定画面で移行してください。",
    );
  }

  // 3. MK 復号
  const blob = b64decode(cfg.api_key_blob);
  const iv = b64decode(cfg.api_key_iv);
  const decryptResult = await client.decrypt(blob, iv);
  const plaintextBytes = decryptResult.plaintext;
  const apiKey = new TextDecoder().decode(plaintextBytes);
  try { plaintextBytes.fill(0); } catch (_e) { /* ignore */ }

  // 4. LLM 呼出
  const provider = cfg.provider;
  const model = cfg.model_name
    || promptContext.default_model_by_provider?.[provider]
    || "";
  if (!model) {
    throw new Error(`unsupported provider for client-side extraction: ${provider}`);
  }
  const extractRes = await runWebExtractImpl({
    promptContext, provider, apiKey, model,
    paymentAccountName, rawText, fetchImpl: f,
  });

  // 5. session 保存 + リダイレクト URL 受領
  const saveRes = await _saveParsedToSession(f, {
    parsed_transactions: extractRes.transactions,
    payment_account_code: paymentAccountCode,
  });

  return {
    transactions: extractRes.transactions,
    usage: extractRes.usage,
    redirect_url: saveRes.redirect_url,
  };
}
