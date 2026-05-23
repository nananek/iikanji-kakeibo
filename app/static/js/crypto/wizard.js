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
  touchWrappedKey,
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
import { beginPasskeyKeyDerivation } from "./passkey_flow.js";


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
    // ロック解除 (unlock) 用ステート
    unlockingKey: null,        // 解除対象の wrapped_key オブジェクト
    unlockPassphrase: "",      // パスフレーズ方式の入力
    unlockMnemonic: "",        // リカバリシード方式の入力
    // 内部ハンドル (非リアクティブ)
    _client: null,

    async init() {
      try {
        this._client = new SharedCryptoClient(getSharedWorkerUrl());
        // MK 状態変化を購読 (他タブの解除 / 60 分 idle ロック等)
        this._client.on("mkChanged", () => { this.hasKey = true; });
        this._client.on("mkCleared", () => {
          this.hasKey = false;
          // 解除フォーム表示中に他タブで unlock されたら閉じる
          if (this.step === "unlock") this.step = "start";
        });
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
     * 解除画面に遷移。ユーザーが任意の登録済 wrapped_key を選んで解除する。
     */
    goToUnlock(wrappedKey) {
      this.error = "";
      this.unlockingKey = wrappedKey;
      this.unlockPassphrase = "";
      this.unlockMnemonic = "";
      this.step = "unlock";
    },

    /** ロック (SharedWorker から MK 消去)。テスト/手動ロック用。 */
    async lockKey() {
      this.error = "";
      try {
        await this._client.clearKey();
        // mkCleared event ハンドラが hasKey=false を反映する
      } catch (e) {
        this.error = `ロック失敗: ${e?.message || e}`;
      }
    },

    /**
     * unwrap 後の共通処理: touchWrappedKey で last_used_at 更新、
     * step を "start" に戻す、入力をクリア。
     */
    async _onUnlockSuccess() {
      try {
        await touchWrappedKey(this.unlockingKey.id);
      } catch (_e) {
        // touch 失敗は致命的でない (UI 表示は古いまま)
      }
      this.unlockPassphrase = "";
      this.unlockMnemonic = "";
      this.unlockingKey = null;
      // hasKey は mkChanged ハンドラ経由で更新済み
      this.step = "start";
      // 一覧の last_used_at を最新化するため再取得
      try {
        this.existingKeys = await listWrappedKeys();
      } catch (_e) { /* ignore */ }
    },

    /** パスフレーズ方式でロック解除。 */
    async unlockWithPassphrase() {
      this.error = "";
      if (!this.unlockingKey || this.unlockingKey.method !== "passphrase") {
        this.error = "不正な状態 (パスフレーズ方式ではない鍵)";
        return;
      }
      if (!this.unlockPassphrase) {
        this.error = "パスフレーズを入力してください";
        return;
      }
      this.loading = true;
      let derived = null;
      try {
        await loadHashWasm();
        const wk = this.unlockingKey;
        derived = await deriveKeyFromPassphrase(
          this.unlockPassphrase,
          wk.salt,
          { params: wk.kdf_params ?? undefined },
        );
        await this._client.unwrap(derived, wk.wrapped_master_key, wk.wrap_iv);
        await this._onUnlockSuccess();
      } catch (e) {
        // unwrap 失敗 = パスフレーズ誤り (タグ検証 NG) が大半
        this.error = "解除に失敗しました。パスフレーズが正しいか確認してください。";
      } finally {
        if (derived && derived.byteLength > 0) {
          try { derived.fill(0); } catch (_e) { /* detached */ }
        }
        this.loading = false;
      }
    },

    /** リカバリシード方式でロック解除。 */
    async unlockWithRecovery() {
      this.error = "";
      if (!this.unlockingKey || this.unlockingKey.method !== "recovery_seed") {
        this.error = "不正な状態 (リカバリシード方式ではない鍵)";
        return;
      }
      if (!this.unlockMnemonic.trim()) {
        this.error = "24 単語を入力してください";
        return;
      }
      this.loading = true;
      let derived = null;
      try {
        const wk = this.unlockingKey;
        derived = await deriveKeyFromMnemonic(this.unlockMnemonic);
        await this._client.unwrap(derived, wk.wrapped_master_key, wk.wrap_iv);
        await this._onUnlockSuccess();
      } catch (e) {
        // mnemonic checksum 不一致 or unwrap タグ NG
        this.error = "解除に失敗しました。24 単語が正しいか確認してください。";
      } finally {
        if (derived && derived.byteLength > 0) {
          try { derived.fill(0); } catch (_e) { /* detached */ }
        }
        this.loading = false;
      }
    },

    /** Passkey 方式でロック解除。 */
    async unlockWithPasskey() {
      this.error = "";
      if (!this.unlockingKey || this.unlockingKey.method !== "passkey_prf") {
        this.error = "不正な状態 (Passkey 方式ではない鍵)";
        return;
      }
      this.loading = true;
      let derived = null;
      try {
        const wk = this.unlockingKey;
        const result = await beginPasskeyKeyDerivation({
          credentialId: wk.webauthn_credential_id,
        });
        derived = result.derivedKey;
        await this._client.unwrap(derived, wk.wrapped_master_key, wk.wrap_iv);
        await this._onUnlockSuccess();
      } catch (e) {
        // 情報漏洩防止: 内部メッセージ (Worker タイムアウト・AES-GCM タグ NG
        // 等) はそのまま UI に出さず、汎用メッセージに統一する。
        // WebAuthn キャンセル (NotAllowedError) のみユーザー操作の意図が
        // 明確なので区別可能にする。
        if (e?.name === "NotAllowedError") {
          this.error = "Passkey 認証がキャンセルされました。";
        } else {
          this.error = "解除に失敗しました。正しい Passkey を使用しているか確認してください。";
        }
      } finally {
        if (derived && derived.byteLength > 0) {
          try { derived.fill(0); } catch (_e) { /* detached */ }
        }
        this.loading = false;
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

    /**
     * Passkey 方式の選択。WebAuthn PRF 拡張で derived_key を派生する。
     * 既存登録済みの Passkey が前提 (新規 Passkey 登録は /settings/passkeys)。
     */
    async selectPasskey() {
      this.error = "";
      this.loading = true;
      let workerKeyGenerated = false;
      // derivedKey を外側 try のスコープに引き上げて、generateKey 等で
      // 失敗した場合でも必ずゼロ埋めできるようにする (PR #143 申し送り対応)
      let derivedKey = null;
      try {
        await this._ensureNewKeySafe();
        // 1. Passkey PRF 認証 → derived_key + credential DB PK
        const result = await beginPasskeyKeyDerivation();
        derivedKey = result.derivedKey;
        const credentialDbId = result.credentialDbId;
        // 2. SharedWorker で新規 MK 生成
        await this._client.generateKey();
        workerKeyGenerated = true;
        // 3. derived_key で MK を wrap (Transferable で detach される)
        const { wrapped, iv } = await this._client.wrap(derivedKey);
        // 4. wrapped_keys に登録 (method=passkey_prf + webauthn_credential_id)
        await createWrappedKey({
          method: "passkey_prf",
          wrapped_master_key: wrapped,
          wrap_iv: iv,
          salt: null,
          kdf_params: null,
          webauthn_credential_id: credentialDbId,
          label: "Passkey (初回)",
        });
        workerKeyGenerated = false;
        this.doneMethod = "passkey_prf";
        this.step = "done";
        this.hasKey = true;
      } catch (e) {
        this.error = e?.message || String(e);
        if (workerKeyGenerated) {
          await this._rollbackWorkerKey();
          this.hasKey = false;
        }
      } finally {
        // derivedKey は wrap で Transferable detach される想定だが、
        // generateKey 失敗等で wrap 到達前なら GC まで残るためゼロ埋め
        if (derivedKey && derivedKey.byteLength > 0) {
          try { derivedKey.fill(0); } catch (_e) { /* detached */ }
        }
        this.loading = false;
      }
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

    /**
     * 方式選択画面に戻る (recovery/passphrase ステップから)。
     * 直接 `step = 'choose'` を代入するとセンシティブ情報 (mnemonic /
     * passphrase) が Alpine reactive data に残る。必ずこの helper を介する。
     */
    goToChoose() {
      this.error = "";
      this.passphrase = "";
      this.passphraseConfirm = "";
      this.mnemonic = "";
      this.mnemonicAcked = false;
      this.step = "choose";
    },
  };
}

// グローバル Alpine.data 登録 (テンプレート側で x-data="encryptionKeyWizard()")
if (typeof globalThis !== "undefined") {
  globalThis.encryptionKeyWizard = encryptionKeyWizard;
}
