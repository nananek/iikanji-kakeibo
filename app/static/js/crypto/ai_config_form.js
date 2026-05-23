// AI 設定画面の E2EE 化フォーム (E2 Phase E2-b)。
//
// 旧 HTML フォーム POST → settings.ai_config_save (Fernet サーバ暗号化) を
// クライアント側 AES-256-GCM 暗号化 + PUT /api/v1/ai-config に置換する。
//
// ユーザー状態の分岐:
//   1. wrapped_keys なし → 鍵設定誘導
//   2. wrapped_keys あり + MK ロード済み + legacy key あり → 移行ボタン表示
//   3. wrapped_keys あり + MK ロード済み + legacy key なし → 通常入力 + 保存
//   4. wrapped_keys あり + MK 未ロード → ロック解除誘導
//
// セキュリティ:
//   - api_key 平文は SharedWorker.encrypt() で MK 暗号化されてから PUT
//   - サーバは復号できない (api_key_blob + api_key_iv のみ受け取る)
//   - 旧 Fernet データは migrate-key 経由で 1 回限り取得 → 再暗号化 → PUT
//   - 入力中 api_key は string で JS GC に委ねるが、encrypt 後の Uint8Array
//     は worker 側で Transferable detach されてゼロ埋め保証

import { SharedCryptoClient } from "./shared-client.js";
import { utf8 } from "./client.js";


// 設定画面が単一インスタンスを使うように window グローバル
function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function b64encode(bytes) {
  let s = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s);
}

function b64decode(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


/** PUT /api/v1/ai-config に暗号文を送信。 */
async function _putAiConfig(body) {
  const r = await fetch("/api/v1/ai-config", {
    method: "PUT",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
    },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${r.status}`);
  }
  return r.json();
}


/** POST /api/v1/ai-config/migrate-key で legacy 平文取得。 */
async function _callMigrateKey() {
  const r = await fetch("/api/v1/ai-config/migrate-key", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
    },
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${r.status}`);
  }
  return r.json();
}


/** GET /api/v1/ai-config (404 は null 返却に正規化)。 */
async function _getAiConfig() {
  const r = await fetch("/api/v1/ai-config", { credentials: "include" });
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}


async function _getWrappedKeysCount() {
  const r = await fetch("/api/v1/wrapped-keys", { credentials: "include" });
  if (!r.ok) return 0;
  const body = await r.json();
  return (body.wrapped_keys || []).length;
}


/**
 * Alpine.data コンポーネント。`<div x-data="aiConfigForm({...})">` で初期化。
 *
 * @param {Object} initial — サーバ render 時に既存設定を埋め込む
 *   {provider, model_name, custom_prompt, compliance_check}
 */
export function aiConfigForm(initial = {}) {
  return {
    // ユーザー状態
    state: "loading",  // "loading"|"no_keys"|"locked"|"need_migration"|"ready"
    error: "",
    notice: "",

    // フォーム入力
    provider: initial.provider || "openai",
    apiKey: "",
    modelName: initial.model_name || "",
    customPrompt: initial.custom_prompt || "",
    complianceCheck: !!initial.compliance_check,
    saving: false,
    migrating: false,

    // サーバ状態
    hasConfig: false,
    hasLegacyKey: false,
    isE2ee: false,

    // 内部
    _client: null,
    // mkChanged listener の累積防止。init() が複数回呼ばれても 1 度だけ登録。
    _mkChangedUnsub: null,

    async init() {
      try {
        if (this._client === null) {
          this._client = new SharedCryptoClient(getSharedWorkerUrl());
        }

        const keyCount = await _getWrappedKeysCount();
        if (keyCount === 0) {
          this.state = "no_keys";
          return;
        }

        const status = await this._client.status();
        if (!status.hasKey) {
          this.state = "locked";
          // mkChanged 解除時に再初期化。リスナー累積防止のため、初回のみ登録
          // (on() の戻り値で unsubscribe 可能だが、本コンポーネントは
          //  ページ寿命と一致するので flag ガードで十分)。
          if (this._mkChangedUnsub === null) {
            this._mkChangedUnsub = this._client.on("mkChanged", () => this.init());
          }
          return;
        }

        // MK ロード済み → 既存設定取得
        const cfg = await _getAiConfig();
        if (cfg) {
          this.hasConfig = true;
          this.hasLegacyKey = !!cfg.has_legacy_key;
          this.isE2ee = !!cfg.is_e2ee;
          this.provider = cfg.provider || this.provider;
          this.modelName = cfg.model_name || "";
          this.customPrompt = cfg.custom_prompt || "";
          this.complianceCheck = !!cfg.compliance_check;
          if (this.hasLegacyKey && !this.isE2ee) {
            this.state = "need_migration";
            return;
          }
        }
        this.state = "ready";
      } catch (e) {
        this.error = `初期化エラー: ${e?.message || e}`;
        this.state = "ready"; // フォームは出すが警告つき
      }
    },

    /** 既存 legacy Fernet 形式から E2EE 形式へ移行する。 */
    async migrate() {
      this.error = "";
      this.notice = "";
      this.migrating = true;
      try {
        // 1. migrate-key 互換 endpoint で平文取得 (per-user 1 回限り)
        const legacy = await _callMigrateKey();
        const plaintextApiKey = legacy.api_key;
        // 2. MK で再暗号化 + PUT
        await this._saveEncrypted({
          apiKey: plaintextApiKey,
          provider: legacy.provider,
          modelName: legacy.model_name || "",
          customPrompt: legacy.custom_prompt || "",
          complianceCheck: !!legacy.compliance_check,
        });
        // 3. フォーム状態を migrate 結果に同期 (古い initial 値を上書き)
        this.provider = legacy.provider || this.provider;
        this.modelName = legacy.model_name || "";
        this.customPrompt = legacy.custom_prompt || "";
        this.complianceCheck = !!legacy.compliance_check;
        this.notice = "暗号化形式を E2EE に移行しました。";
        this.hasConfig = true;
        this.hasLegacyKey = false;
        this.isE2ee = true;
        this.state = "ready";
      } catch (e) {
        this.error = `移行に失敗しました: ${e?.message || e}`;
      } finally {
        this.migrating = false;
      }
    },

    /** フォーム入力 → 暗号化 + PUT。 */
    async save() {
      this.error = "";
      this.notice = "";
      const isLlamaCpp = this.provider === "llama_cpp";
      // llama_cpp はサーバ管理者提供 API なので API キー不要。
      // 新規登録時の API キー必須は llama_cpp 以外のみ。
      if (!isLlamaCpp && !this.apiKey && !this.isE2ee) {
        this.error = "API キーを入力してください。";
        return;
      }
      this.saving = true;
      try {
        if (isLlamaCpp) {
          // llama_cpp は API キー文字列を持たないが、API スキーマは blob/iv
          // 必須なので空文字列を暗号化して送る (= 復号して "" を得る)。
          // クライアント (Web) が llama_cpp 呼出時は MK で復号 → 空文字列を
          // 取得 → 「サーバ管理者提供 LLM を使う」フラグとして扱う。
          await this._saveEncrypted({
            apiKey: "",
            provider: this.provider,
            modelName: this.modelName,
            customPrompt: this.customPrompt,
            complianceCheck: this.complianceCheck,
          });
          this.notice = "設定を保存しました (llama_cpp、API キー不要)。";
        } else if (!this.apiKey) {
          // 既存設定の更新で api_key を変更しない場合は現在の blob/iv を維持
          // → PUT は blob/iv 必須なため、GET で取得して流用する
          const cur = await _getAiConfig();
          if (!cur || !cur.api_key_blob || !cur.api_key_iv) {
            this.error = "API キーを入力してください (既存暗号文が見つかりません)";
            return;
          }
          await _putAiConfig({
            provider: this.provider,
            api_key_blob: cur.api_key_blob,
            api_key_iv: cur.api_key_iv,
            model_name: this.modelName,
            custom_prompt: this.customPrompt,
            compliance_check: this.complianceCheck,
          });
          this.notice = "設定を保存しました (API キー再利用)。";
        } else {
          await this._saveEncrypted({
            apiKey: this.apiKey,
            provider: this.provider,
            modelName: this.modelName,
            customPrompt: this.customPrompt,
            complianceCheck: this.complianceCheck,
          });
          this.notice = "設定を保存しました。";
        }
        // 入力欄をクリア (再入力時の漏洩防止)
        this.apiKey = "";
        this.hasConfig = true;
        this.isE2ee = true;
        this.hasLegacyKey = false;
      } catch (e) {
        this.error = `保存に失敗しました: ${e?.message || e}`;
      } finally {
        this.saving = false;
      }
    },

    /** api_key を MK で暗号化して PUT (共通処理)。 */
    async _saveEncrypted({ apiKey, provider, modelName, customPrompt, complianceCheck }) {
      // UTF-8 化 + AES-GCM 暗号化。Transferable 経由でメインスレッドから detach
      const ptBytes = utf8(apiKey);
      const { ciphertext, iv } = await this._client.encrypt(ptBytes);
      await _putAiConfig({
        provider,
        api_key_blob: b64encode(ciphertext),
        api_key_iv: b64encode(iv),
        model_name: modelName,
        custom_prompt: customPrompt,
        compliance_check: complianceCheck,
      });
    },

    async deleteConfig() {
      if (!window.confirm("外部 AI 設定を削除しますか?")) return;
      this.error = "";
      try {
        const r = await fetch("/api/v1/ai-config", {
          method: "DELETE",
          credentials: "include",
          headers: { "X-CSRFToken": csrfToken() },
        });
        if (!r.ok && r.status !== 204) {
          throw new Error(`HTTP ${r.status}`);
        }
        this.notice = "設定を削除しました。";
        this.hasConfig = false;
        this.hasLegacyKey = false;
        this.isE2ee = false;
        this.provider = "openai";
        this.modelName = "";
        this.customPrompt = "";
        this.complianceCheck = false;
        this.apiKey = "";
      } catch (e) {
        this.error = `削除に失敗しました: ${e?.message || e}`;
      }
    },
  };
}

// Alpine.data('aiConfigForm', aiConfigForm) はテンプレート側で
// `alpine:init` イベントに登録する (プロジェクトの標準パターン)。
// globalThis 代入は読込順依存で脆いため使わない。
