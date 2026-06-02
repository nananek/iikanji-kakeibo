// AI 設定画面の E2EE 化フォーム。
//
// クライアント側 AES-256-GCM で API キーを暗号化 → PUT /api/v1/ai-config。
// サーバは復号できない (api_key_blob + api_key_iv のみ受け取る)。
//
// ユーザー状態の分岐:
//   1. wrapped_keys なし → 鍵設定誘導
//   2. wrapped_keys あり + MK ロード済み → 通常入力 + 保存
//   3. wrapped_keys あり + MK 未ロード → ロック解除誘導
//
// セキュリティ:
//   - api_key 平文は SharedWorker.encrypt() で MK 暗号化されてから PUT
//   - 入力中 api_key は string で JS GC に委ねるが、encrypt 後の Uint8Array
//     は worker 側で Transferable detach されてゼロ埋め保証

import { SharedCryptoClient } from "./shared-client.js";
import { utf8 } from "./client.js";
import { b64encode } from "./b64.js";


// 設定画面が単一インスタンスを使うように window グローバル
function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
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
    state: "loading",  // "loading"|"no_keys"|"locked"|"ready"
    error: "",
    notice: "",

    // フォーム入力
    provider: initial.provider || "openai",
    apiKey: "",
    modelName: initial.model_name || "",
    // 「現在サーバに保存されている provider」のスナップショット。
    // 切替検出に使い、provider 変更時に新 API キー入力を強制する。
    _savedProvider: null,
    customPrompt: initial.custom_prompt || "",
    complianceCheck: !!initial.compliance_check,
    saving: false,

    // サーバ状態
    hasConfig: false,
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
          this.isE2ee = !!cfg.is_e2ee;
          this.provider = cfg.provider || this.provider;
          this._savedProvider = cfg.provider || null;
          this.modelName = cfg.model_name || "";
          this.customPrompt = cfg.custom_prompt || "";
          this.complianceCheck = !!cfg.compliance_check;
        }
        this.state = "ready";
      } catch (e) {
        this.error = `初期化エラー: ${e?.message || e}`;
        // _client が null のままフォームを出すと save() で TypeError になるため
        // loading のままに留めてフォーム非表示にする。ユーザーは error バナーを
        // 見て再読込する。
        this.state = "loading";
      }
    },

    /** フォーム入力 → 暗号化 + PUT。 */
    async save() {
      this.error = "";
      this.notice = "";
      if (!this.apiKey && !this.hasConfig) {
        this.error = "API キーを入力してください。";
        return;
      }
      // provider 切替時に既存 blob (= 前 provider 用の暗号化キー) を流用するのは
      // 危険。新 API キー必須化。
      const providerChanged =
        this._savedProvider !== null && this._savedProvider !== this.provider;
      if (providerChanged && !this.apiKey) {
        this.error = (
          `プロバイダーを変更しました (${this._savedProvider} → ${this.provider})。` +
          " 新しい API キーを入力してください。"
        );
        return;
      }
      this.saving = true;
      try {
        if (!this.apiKey) {
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
        // 保存に成功した provider をスナップショット (次回の切替検出用)
        this._savedProvider = this.provider;
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
        // 注: /api/v1/* は CSRF 免除済みだが、X-CSRFToken は belt-and-suspenders
        // として送り続ける (将来 CSRF 免除を外した場合の後方互換)
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
