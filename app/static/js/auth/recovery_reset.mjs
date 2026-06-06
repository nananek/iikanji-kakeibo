// #385 PR-4b-3: リカバリシードによるパスワードリセット (設計書 §3.4.1)。
//
// パスワードを忘れたユーザーが 24 語のリカバリシードで MK を unwrap し、新パスワードを
// 設定する。シードは「フル復旧因子」なので、リセット時に**新しいシードへローテーション**し、
// 完了後に新シードを 1 回限りセキュア表示する。サーバには平文シード/パスワードを送らない。
//
//   1. POST /auth/recovery/begin {username}     → recovery_seed wrapped_key を取得
//   2. 旧シードで MK を unwrap (= シード検証) + recovery_verifier 導出
//   3. 新パスワードで新 mk_wrap_key 導出 → MK を再 wrap (passphrase)
//   4. 新シード生成 → MK を新シードで再 wrap + new_recovery_verifier 導出 (ローテ)
//   5. POST /auth/recovery/finish {...}          → 単一 Tx で全更新
//   6. 新シードをセキュア表示 → 新パスワードでログインし直す
//
// 重い暗号は既存テスト済みモジュールに委譲し、本モジュールはオーケストレーションに集中する。
// runRecoveryReset は依存注入可能で単体テスト可能。

import { b64encode, b64decode } from "../crypto/b64.js";
import { deriveLoginMaterial } from "../crypto/login_kdf.js";
import {
  deriveKeyFromMnemonic,
  deriveRecoveryVerifier,
  generateMnemonic,
} from "../crypto/bip39.js";
import { generateSalt, ARGON2ID_DEFAULTS } from "../crypto/argon2.js";
import { loadHashWasm } from "../crypto/hash_wasm_loader.js";
import { SharedCryptoClient } from "../crypto/shared-client.js";

const KDF_PARAMS = {
  memory: ARGON2ID_DEFAULTS.memorySize,
  iterations: ARGON2ID_DEFAULTS.iterations,
  parallelism: ARGON2ID_DEFAULTS.parallelism,
};


function _zero(buf) {
  try { if (buf && buf.byteLength > 0) buf.fill(0); } catch (_e) { /* detached */ }
}


/**
 * リカバリシードでパスワードをリセットし、シードをローテーションする。
 *
 * 返り値:
 *   {status:"ok", newMnemonic}   成功 (呼び出し側が新シードをセキュア表示)
 *   {status:"invalid_seed"}      シード形式不正 (チェックサム NG)
 *   {status:"wrong_seed"}        シードが当該アカウントのものでない (unwrap/照合失敗)
 *   {status:"error", message}    その他
 *
 * @param {Object} o
 * @param {string} o.username
 * @param {string} o.mnemonic        ユーザーが入力した 24 語
 * @param {string} o.newPassword
 * @param {Object} o.client          SharedCryptoClient
 * @param {Object} [o.deps]          テスト用 DI
 */
export async function runRecoveryReset({ username, mnemonic, newPassword, client, deps = {} }) {
  const f = deps.fetchImpl ?? globalThis.fetch;
  const _loadHashWasm = deps.loadHashWasm ?? loadHashWasm;
  const _deriveLogin = deps.deriveLoginMaterial ?? deriveLoginMaterial;
  const _deriveSeed = deps.deriveKeyFromMnemonic ?? deriveKeyFromMnemonic;
  const _deriveRecoveryVerifier = deps.deriveRecoveryVerifier ?? deriveRecoveryVerifier;
  const _generateMnemonic = deps.generateMnemonic ?? generateMnemonic;
  const _generateSalt = deps.generateSalt ?? generateSalt;

  if (!username) {
    return { status: "error", message: "ユーザー名を入力してください。" };
  }
  if (!newPassword || newPassword.length < 8) {
    return { status: "error", message: "新しいパスワードは 8 文字以上にしてください。" };
  }

  await _loadHashWasm();

  // 1. begin: recovery_seed wrapped_key を取得 (未知/未設定ユーザーには決定的ダミーが返る)。
  let beginResp;
  try {
    const r = await f("/auth/recovery/begin", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!r.ok) return { status: "error", message: "リセットを開始できませんでした。" };
    beginResp = await r.json();
  } catch (_e) {
    return { status: "error", message: "リセットを開始できませんでした。" };
  }

  // 2. 旧シードで鍵導出 (形式不正は invalid_seed)。
  let seedKey, recoveryVerifier;
  try {
    seedKey = await _deriveSeed(mnemonic);
    recoveryVerifier = await _deriveRecoveryVerifier(mnemonic);
  } catch (_e) {
    return { status: "invalid_seed" };
  }

  const newSalt = _generateSalt();
  let newMat, newMnemonic, newSeedKey, newRecoveryVerifier;
  try {
    // 旧シードで MK を unwrap (= シード検証)。失敗 = シード不一致 (ダミー含む)。
    try {
      await client.unwrap(
        seedKey,
        b64decode(beginResp.wrapped_master_key),
        b64decode(beginResp.wrap_iv),
      );
    } catch (_e) {
      return { status: "wrong_seed" };
    }

    // 3. 新パスワードで新 material を導出し MK を passphrase 鍵で再 wrap。
    newMat = await _deriveLogin(newPassword, newSalt, {});
    const ppWrap = await client.wrap(newMat.mkWrapKey); // {wrapped, iv}

    // 4. 新シードを生成し MK を再 wrap + new_recovery_verifier 導出 (シードローテ)。
    newMnemonic = await _generateMnemonic();
    newSeedKey = await _deriveSeed(newMnemonic);
    newRecoveryVerifier = await _deriveRecoveryVerifier(newMnemonic);
    const recWrap = await client.wrap(newSeedKey); // {wrapped, iv}

    // 5. finish: 単一トランザクションで全更新。
    const resp = await f("/auth/recovery/finish", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username,
        recovery_verifier: b64encode(recoveryVerifier),
        login_verifier: b64encode(newMat.loginVerifier),
        login_salt: b64encode(newSalt),
        login_kdf_params: KDF_PARAMS,
        passphrase_wrapped_master_key: b64encode(ppWrap.wrapped),
        passphrase_wrap_iv: b64encode(ppWrap.iv),
        recovery_wrapped_master_key: b64encode(recWrap.wrapped),
        recovery_wrap_iv: b64encode(recWrap.iv),
        new_recovery_verifier: b64encode(newRecoveryVerifier),
      }),
    });
    if (resp.status === 401) {
      // unwrap は通ったが verifier 照合 NG = サーバ側ハッシュ未設定 (旧ウィザード) 等。
      return { status: "wrong_seed" };
    }
    if (!resp.ok) {
      return { status: "error", message: "リセットに失敗しました。" };
    }
    // 6. 新シードを呼び出し側へ (セキュア表示用)。
    return { status: "ok", newMnemonic };
  } catch (e) {
    console.error("recovery reset error", e);
    return { status: "error", message: "リセットに失敗しました。" };
  } finally {
    _zero(newSalt);
    _zero(seedKey);
    _zero(recoveryVerifier);
    if (newMat) { _zero(newMat.loginVerifier); _zero(newMat.mkWrapKey); }
    if (newRecoveryVerifier) _zero(newRecoveryVerifier);
    // newSeedKey は client.wrap で detach 済。newMnemonic は呼び出し側が表示後に破棄。
  }
}


// ------------------------------------------------------------------
// DOM 配線 (ブラウザのみ)
// ------------------------------------------------------------------

function _setMsg(el, kind, text) {
  if (!el) return;
  const cls = { ok: "text-success", error: "text-danger", info: "text-info" }[kind] || "";
  el.className = "mt-3 small " + cls;
  el.textContent = text;
  el.classList.remove("d-none");
}


function _showNewSeed(mnemonic) {
  const formCard = document.getElementById("reset-form-card");
  const doneCard = document.getElementById("reset-done-card");
  const grid = document.getElementById("new-seed-grid");
  if (!doneCard || !grid) return;
  // DOM API で描画 (テンプレートリテラル + innerHTML を避け XSS 面をなくす)。
  grid.textContent = "";
  mnemonic.split(" ").forEach((word, i) => {
    const cell = document.createElement("div");
    cell.className = "col-4 col-md-3";
    const badge = document.createElement("span");
    badge.className = "badge bg-secondary me-1";
    badge.textContent = String(i + 1);
    cell.appendChild(badge);
    cell.appendChild(document.createTextNode(word));
    grid.appendChild(cell);
  });
  if (formCard) formCard.classList.add("d-none");
  doneCard.classList.remove("d-none");
}


function _bind() {
  const form = document.getElementById("recovery-reset-form");
  if (!form) return;
  const statusEl = document.getElementById("reset-status");

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const username = form.querySelector('[name="username"]')?.value.trim() || "";
    const mnemonic = form.querySelector('[name="mnemonic"]')?.value.trim() || "";
    const newEl = form.querySelector('[name="new_password"]');
    const confirmEl = form.querySelector('[name="new_password_confirm"]');
    const newPassword = newEl?.value || "";
    if (!username || !mnemonic || !newPassword) {
      _setMsg(statusEl, "error", "すべての項目を入力してください。");
      return;
    }
    if (newPassword !== (confirmEl?.value || "")) {
      _setMsg(statusEl, "error", "新しいパスワード (確認) が一致しません。");
      return;
    }
    const btn = form.querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    _setMsg(statusEl, "info", "リセットしています…（鍵を再ラップ中）");

    const workerUrl = globalThis.IIKANJI_SHARED_WORKER_URL
      || "/static/js/crypto/shared-worker.js";
    const client = new SharedCryptoClient(workerUrl);
    let result;
    try {
      result = await runRecoveryReset({ username, mnemonic, newPassword, client });
    } catch (e) {
      console.error("recovery reset error", e);
      result = { status: "error", message: "リセットに失敗しました。" };
    } finally {
      // 入力欄をクリア (シード/パスワードを DOM に残さない)。
      [newEl, confirmEl].forEach((el) => { if (el) el.value = ""; });
      const mEl = form.querySelector('[name="mnemonic"]');
      if (mEl) mEl.value = "";
      try { client.close(); } catch (_e) { /* ignore */ }
    }
    if (result.status === "ok") {
      _showNewSeed(result.newMnemonic);
    } else if (result.status === "invalid_seed") {
      _setMsg(statusEl, "error", "リカバリシードの形式が正しくありません (24 語を確認してください)。");
      if (btn) btn.disabled = false;
    } else if (result.status === "wrong_seed") {
      _setMsg(statusEl, "error", "このアカウントのリカバリシードと一致しません。");
      if (btn) btn.disabled = false;
    } else {
      _setMsg(statusEl, "error", result.message || "リセットに失敗しました。");
      if (btn) btn.disabled = false;
    }
  });
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bind);
  } else {
    _bind();
  }
}
