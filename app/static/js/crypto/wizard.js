// 鍵設定ウィザード Alpine.js コンポーネント (E1 PR-F2)。
//
// フロー (3 ステップ wizard):
//   1. start    : status() で hasKey 確認 → 既存鍵あり/なしで分岐
//   2. choose   : 方式選択 (passphrase / recovery_seed / passkey 将来)
//   3. configure: 入力フォーム + 派生 → wrap → API POST
//   4. done     : 完了画面 (リカバリシード方式は表示画面で「メモした」確認)
//
// 重要セキュリティ判断:
// - generateKey 前に必ず client.status() を呼んで hasKey=false を確認
//   (既存 MK 上書きで暗号文を破壊するリスク回避)
// - パスフレーズ / mnemonic は確認入力後すぐにスコープ外へ (string GC)
// - derived_key は wrap 直後にゼロ埋め (worker.js でも実施しているが二重防御)

import { SharedCryptoClient } from "./shared-client.js";
import {
  createWrappedKey,
  listWrappedKeys,
} from "./api.js";
import {
  ARGON2ID_DEFAULTS,
  deriveKeyFromPassphrase,
  generateSalt,
} from "./argon2.js";
import {
  deriveKeyFromMnemonic,
  generateMnemonic,
} from "./bip39.js";
import { loadHashWasm } from "./hash_wasm_loader.js";


// SharedWorker URL は base.html 等で `window.IIKANJI_SHARED_WORKER_URL` に
// 注入してもらう。ウィザード単独で初期化する場合はデフォルト URL。
function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


/**
 * Alpine.data コンポーネント。`<div x-data="encryptionKeyWizard()">` で初期化。
 *
 * 状態:
 *   step          : "start" | "choose" | "passphrase" | "recovery" | "done"
 *   loading       : 処理中フラグ (Argon2id は数秒かかる)
 *   error         : エラーメッセージ
 *   existingKeys  : 登録済 wrapped_keys 一覧
 *   hasKey        : SharedWorker 側 MK 設定状況
 *   passphrase    : 入力中パスフレーズ
 *   passphraseConfirm: 確認用
 *   mnemonic      : 表示中ニーモニック (生成済の場合)
 *   mnemonicAcked : 「紙にメモした」チェックボックス
 *   doneMethod    : 完了直後の方式表示用
 */
export function encryptionKeyWizard() {
  return {
    step: "start",
    loading: false,
    error: "",
    existingKeys: [],
    hasKey: false,
    passphrase: "",
    passphraseConfirm: "",
    mnemonic: "",
    mnemonicAcked: false,
    doneMethod: "",
    // 内部ハンドル (非リアクティブ)
    _client: null,

    async init() {
      try {
        this._client = new SharedCryptoClient(getSharedWorkerUrl());
        const [status, keys] = await Promise.all([
          this._client.status(),
          listWrappedKeys(),
        ]);
        this.hasKey = !!status.hasKey;
        this.existingKeys = keys;
      } catch (e) {
        this.error = `初期化エラー: ${e?.message || e}`;
      }
    },

    /**
     * 新規 MK を SharedWorker 内で生成する前に必ず status() で hasKey=false
     * を確認する。これは PR #139 review の申し送り事項。
     *
     * **TOCTOU 対策**: `this.existingKeys` (init 時キャッシュ) ではなく、
     * 都度 `listWrappedKeys()` を再取得する。別タブ・並行リクエストで鍵が
     * 登録された場合にチェックをすり抜ける問題を防ぐ。
     */
    async _ensureNewKeySafe() {
      const [s, freshKeys] = await Promise.all([
        this._client.status(),
        listWrappedKeys(),
      ]);
      if (s.hasKey) {
        throw new Error(
          "既に MK が SharedWorker にロードされています。設定済みの鍵を上書きすると既存暗号文が復号不能になります。一度ロックしてからやり直してください。",
        );
      }
      if (freshKeys.length > 0) {
        // キャッシュも最新に揃える (start 画面表示用)
        this.existingKeys = freshKeys;
        throw new Error(
          `既に登録済みの鍵 (${freshKeys.length} 件) があります。新規 MK 生成は最初のセットアップ時のみ可能です。`,
        );
      }
    },

    /**
     * generateKey 後に createWrappedKey などが失敗した時の SharedWorker
     * ロールバック。失敗状態を放置すると hasKey=true / DB に行なし という
     * 矛盾状態でリトライ不能になるため、必ず clearKey する。
     */
    async _rollbackWorkerKey() {
      try {
        await this._client.clearKey();
      } catch (_e) {
        // clearKey 自体の失敗は致命的でないが、UI に通知する手段がないので無視
      }
    },

    proceedChoose() {
      this.error = "";
      this.step = "choose";
    },

    selectPassphrase() {
      this.error = "";
      this.passphrase = "";
      this.passphraseConfirm = "";
      this.step = "passphrase";
    },

    async selectRecovery() {
      this.error = "";
      this.loading = true;
      try {
        this.mnemonic = await generateMnemonic();
        this.mnemonicAcked = false;
        this.step = "recovery";
      } catch (e) {
        this.error = `ニーモニック生成失敗: ${e?.message || e}`;
      } finally {
        this.loading = false;
      }
    },

    async submitPassphrase() {
      this.error = "";
      if (this.passphrase.length < 8) {
        this.error = "パスフレーズは 8 文字以上";
        return;
      }
      if (this.passphrase !== this.passphraseConfirm) {
        this.error = "パスフレーズが一致しません";
        return;
      }
      this.loading = true;
      // generateKey 成功後に後続が失敗したらロールバックすべき
      let workerKeyGenerated = false;
      try {
        await this._ensureNewKeySafe();
        // hash-wasm を CDN からロード (初回のみ実体取得)
        await loadHashWasm();
        await this._client.generateKey();
        workerKeyGenerated = true;
        const salt = generateSalt();
        let derived;
        try {
          derived = await deriveKeyFromPassphrase(this.passphrase, salt);
          // Transferable: derived は wrap 後に detach される
          const { wrapped, iv } = await this._client.wrap(derived);
          await createWrappedKey({
            method: "passphrase",
            wrapped_master_key: wrapped,
            wrap_iv: iv,
            salt,
            kdf_params: { ...ARGON2ID_DEFAULTS },
            webauthn_credential_id: null,
            label: "パスフレーズ (初回)",
          });
        } finally {
          // derived は wrap で detach されているはずだが二重防御
          if (derived && derived.byteLength > 0) {
            try { derived.fill(0); } catch (_e) { /* detached */ }
          }
        }
        // 成功パス: rollback フラグを下ろす
        workerKeyGenerated = false;
        this.passphrase = "";
        this.passphraseConfirm = "";
        this.doneMethod = "passphrase";
        this.step = "done";
        this.hasKey = true;
      } catch (e) {
        this.error = e?.message || String(e);
        // generateKey 成功後の失敗なら Worker の MK を clear して矛盾状態を解消
        if (workerKeyGenerated) {
          await this._rollbackWorkerKey();
          this.hasKey = false;
        }
      } finally {
        this.loading = false;
      }
    },

    async submitRecovery() {
      this.error = "";
      if (!this.mnemonicAcked) {
        this.error = "「24 単語を紙にメモした」を確認してください";
        return;
      }
      this.loading = true;
      let workerKeyGenerated = false;
      try {
        await this._ensureNewKeySafe();
        await this._client.generateKey();
        workerKeyGenerated = true;
        let derived;
        try {
          derived = await deriveKeyFromMnemonic(this.mnemonic);
          const { wrapped, iv } = await this._client.wrap(derived);
          await createWrappedKey({
            method: "recovery_seed",
            wrapped_master_key: wrapped,
            wrap_iv: iv,
            salt: null,
            kdf_params: null,
            webauthn_credential_id: null,
            label: "リカバリシード (初回)",
          });
        } finally {
          if (derived && derived.byteLength > 0) {
            try { derived.fill(0); } catch (_e) { /* detached */ }
          }
        }
        // 成功パス: rollback フラグを下ろす
        workerKeyGenerated = false;
        // Alpine reactive data に mnemonic を残すと DevTools で 24 単語を
        // inspect できてしまうため、成功後すぐに空文字へクリアする。
        this.mnemonic = "";
        this.mnemonicAcked = false;
        this.doneMethod = "recovery_seed";
        this.step = "done";
        this.hasKey = true;
      } catch (e) {
        this.error = e?.message || String(e);
        if (workerKeyGenerated) {
          await this._rollbackWorkerKey();
          this.hasKey = false;
        }
      } finally {
        this.loading = false;
      }
    },

    backToStart() {
      this.error = "";
      this.passphrase = "";
      this.passphraseConfirm = "";
      this.mnemonic = "";
      this.mnemonicAcked = false;
      this.step = "start";
    },
  };
}

// グローバル Alpine.data 登録 (テンプレート側で x-data="encryptionKeyWizard()")
if (typeof globalThis !== "undefined") {
  globalThis.encryptionKeyWizard = encryptionKeyWizard;
}
