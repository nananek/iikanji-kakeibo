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
  deleteWrappedKey,
  listWrappedKeys,
  touchWrappedKey,
} from "./api.js";
import {
  deriveKeyFromMnemonic,
  deriveRecoveryVerifier,
  generateMnemonic,
} from "./bip39.js";
import { loadHashWasm } from "./hash_wasm_loader.js";
import { beginPasskeyKeyDerivation } from "./passkey_flow.js";
import { ensureKeyPair } from "./keypair.js";
import { deriveLoginMaterial } from "./login_kdf.js";


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
export function encryptionKeyWizard(config = {}) {
  // SharedCryptoClient は private フィールド (#port 等) を持つ。Alpine の
  // reactive Proxy にプロパティとして載せると、メソッド内の private フィールド
  // アクセスが "can't access private field or method: object is not the right
  // class" で全滅する (Proxy が brand check を満たさない)。CLAUDE.md の規約
  // どおり**非リアクティブな閉包変数**として保持する。
  let client = null;
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
    // 追加モード: true なら「解錠中の MK を別方式で wrap して鍵を追加」。
    // false (既定) は初回セットアップ (新規 MK 生成)。
    addMode: false,
    // X25519 鍵ペア backfill 用ユーザー ID (テンプレートから注入、§14 / E5 PR-A)
    _userId: config.userId ?? null,

    async init() {
      try {
        client = new SharedCryptoClient(getSharedWorkerUrl());
        // MK 状態変化を購読 (他タブの解除 / 60 分 idle ロック等)
        client.on("mkChanged", () => { this.hasKey = true; });
        client.on("mkCleared", () => {
          this.hasKey = false;
          // 解除フォーム表示中に他タブで unlock されたら閉じる
          if (this.step === "unlock") this.step = "start";
        });
        const [status, keys] = await Promise.all([
          client.status(),
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
        await client.clearKey();
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
      // MK が手元にある今、X25519 鍵ペア未設定なら backfill (§14 / E5 PR-A)
      await this._backfillKeyPair();
      // 一覧の last_used_at を最新化するため再取得
      try {
        this.existingKeys = await listWrappedKeys();
      } catch (_e) { /* ignore */ }
    },

    /**
     * X25519 鍵ペアが未設定なら生成して保管する (best-effort)。MK が
     * SharedWorker にある状態で呼ぶこと。失敗しても鍵設定/解錠フロー自体は
     * 妨げない (設計書の「監査者鍵生成失敗は手動対応」と整合)。userId 未注入
     * (テンプレートが古い等) の場合は no-op。
     */
    async _backfillKeyPair() {
      if (this._userId === null || this._userId === undefined) return;
      try {
        await ensureKeyPair(client, this._userId);
      } catch (e) {
        // 監査連携 (E5) 未利用なら実害なし。次回 MK 解錠時に再試行される。
        console.warn("X25519 鍵ペアの生成/保管に失敗しました:", e?.message || e);
      }
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
      let material = null;
      try {
        await loadHashWasm();
        const wk = this.unlockingKey;
        // #385: passphrase wrapped_key は login_flow が mk_wrap_key = HKDF split(
        // Argon2id(login_password, login_salt)) で wrap している。よって解錠も
        // deriveLoginMaterial の mk_wrap_key で行う (旧 Argon2id 直接派生では
        // 鍵が一致せず解錠できない)。入力するパスフレーズ = ログインパスワード。
        material = await deriveLoginMaterial(
          this.unlockPassphrase,
          wk.salt,
          { params: wk.kdf_params ?? undefined },
        );
        await client.unwrap(material.mkWrapKey, wk.wrapped_master_key, wk.wrap_iv);
        await this._onUnlockSuccess();
      } catch (e) {
        // unwrap 失敗 = パスフレーズ誤り (タグ検証 NG) が大半
        this.error = "解除に失敗しました。パスフレーズが正しいか確認してください。";
      } finally {
        if (material) {
          try { material.mkWrapKey.fill(0); } catch (_e) { /* detached */ }
          try { material.loginVerifier.fill(0); } catch (_e) { /* detached */ }
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
        await client.unwrap(derived, wk.wrapped_master_key, wk.wrap_iv);
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
        await client.unwrap(derived, wk.wrapped_master_key, wk.wrap_iv);
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
        client.status(),
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
        await client.clearKey();
      } catch (_e) {
        // clearKey 自体の失敗は致命的でないが、UI に通知する手段がないので無視
      }
    },

    /**
     * 鍵登録の前に MK を準備する。
     * - 通常 (初回): _ensureNewKeySafe で多重鍵を防ぎ generateKey で新規 MK 生成。
     *   返り値 true (= generateKey した → 失敗時 _rollbackWorkerKey 対象)。
     * - 追加 (addMode): 解錠済みの既存 MK をそのまま wrap する。generateKey せず、
     *   MK 未解錠なら中断。返り値 false (= MK を生成していないので rollback 不要)。
     */
    async _prepareMkForRegister() {
      if (this.addMode) {
        const st = await client.status();
        if (!st.hasKey) {
          throw new Error(
            "鍵を追加するには、先に既存の鍵で暗号鍵を解除してください。",
          );
        }
        return false;
      }
      await this._ensureNewKeySafe();
      await client.generateKey();
      return true;
    },

    /** createWrappedKey 成功後の遷移。追加モードは一覧へ戻り、初回は done へ。 */
    async _afterRegister(method) {
      if (this.addMode) {
        this.addMode = false;
        this.step = "start";
        try {
          this.existingKeys = await listWrappedKeys();
        } catch (_e) { /* ignore */ }
      } else {
        this.doneMethod = method;
        this.step = "done";
        this.hasKey = true;
        // 新規 MK 確立直後に X25519 鍵ペアを生成・保管 (§14 / E5 PR-A)
        await this._backfillKeyPair();
      }
    },

    /** 「別の方式を追加」: 解錠中の MK に別方式の wrapped_key を足す。 */
    addAnotherMethod() {
      this.error = "";
      this.addMode = true;
      this.step = "choose";
    },

    /** wrapped_key を削除する (最終 1 件はサーバが 409 で保護)。 */
    async deleteKey(wk) {
      this.error = "";
      if (!wk || wk.id == null) return;
      const label = wk.label || wk.method || "この鍵";
      if (typeof globalThis.confirm === "function"
          && !globalThis.confirm(
            `「${label}」を削除します。よろしいですか?\n`
            + "(この方式での解除ができなくなります。他に登録済みの方式が必要です)",
          )) {
        return;
      }
      this.loading = true;
      try {
        await deleteWrappedKey(wk.id);
        this.existingKeys = await listWrappedKeys();
      } catch (e) {
        this.error = "鍵の削除に失敗しました: " + (e?.message || e);
      } finally {
        this.loading = false;
      }
    },

    proceedChoose() {
      this.error = "";
      this.step = "choose";
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
        // 1. Passkey PRF 認証 → derived_key + credential DB PK
        const result = await beginPasskeyKeyDerivation();
        derivedKey = result.derivedKey;
        const credentialDbId = result.credentialDbId;
        // 2. 追加モードは既存 MK を wrap、初回は新規 MK 生成。
        workerKeyGenerated = await this._prepareMkForRegister();
        // 3. derived_key で MK を wrap (Transferable で detach される)
        const { wrapped, iv } = await client.wrap(derivedKey);
        // 4. wrapped_keys に登録 (method=passkey_prf + webauthn_credential_id)
        await createWrappedKey({
          method: "passkey_prf",
          wrapped_master_key: wrapped,
          wrap_iv: iv,
          salt: null,
          kdf_params: null,
          webauthn_credential_id: credentialDbId,
          label: this.addMode ? "Passkey (追加)" : "Passkey (初回)",
        });
        workerKeyGenerated = false;
        await this._afterRegister("passkey_prf");
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

    // #385: passphrase 単独登録 (submitPassphrase/selectPassphrase) は廃止。
    // passphrase 由来の wrapped_key は login_flow が HKDF split (mk_wrap_key) で独占
    // 生成する。ウィザードからの raw Argon2id 登録は解錠 (login 派生) と KDF が不整合に
    // なるため削除した。解錠は unlockWithPassphrase (login 派生) が担う。

    async submitRecovery() {
      this.error = "";
      if (!this.mnemonicAcked) {
        this.error = "「24 単語を紙にメモした」を確認してください";
        return;
      }
      this.loading = true;
      let workerKeyGenerated = false;
      try {
        // 追加モードは既存 MK を wrap、初回は新規 MK 生成。
        workerKeyGenerated = await this._prepareMkForRegister();
        let derived;
        let recoveryVerifier;
        try {
          derived = await deriveKeyFromMnemonic(this.mnemonic);
          // #385 PR-4b-1: 同一シードからサーバ照合用 recovery_verifier も導出し、
          // recovery_seed_server_hash を確立する (seed-only リセットを有効化、§3.4.1)。
          recoveryVerifier = await deriveRecoveryVerifier(this.mnemonic);
          const { wrapped, iv } = await client.wrap(derived);
          await createWrappedKey({
            method: "recovery_seed",
            wrapped_master_key: wrapped,
            wrap_iv: iv,
            salt: null,
            kdf_params: null,
            webauthn_credential_id: null,
            recovery_verifier: recoveryVerifier,
            label: this.addMode ? "リカバリシード (追加)" : "リカバリシード (初回)",
          });
        } finally {
          if (derived && derived.byteLength > 0) {
            try { derived.fill(0); } catch (_e) { /* detached */ }
          }
          if (recoveryVerifier && recoveryVerifier.byteLength > 0) {
            try { recoveryVerifier.fill(0); } catch (_e) { /* detached */ }
          }
        }
        // 成功パス: rollback フラグを下ろす
        workerKeyGenerated = false;
        // Alpine reactive data に mnemonic を残すと DevTools で 24 単語を
        // inspect できてしまうため、成功後すぐに空文字へクリアする。
        this.mnemonic = "";
        this.mnemonicAcked = false;
        await this._afterRegister("recovery_seed");
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
      this.addMode = false;
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
