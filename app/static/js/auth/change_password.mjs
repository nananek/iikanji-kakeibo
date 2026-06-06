// #385 PR-4: ログインパスワード変更 (設計書 §3.3)。
//
// MK 自体は不変。旧パスワードで passphrase wrapped_key を unwrap して MK を得 (= 旧
// パスワード検証)、新 salt で新 mk_wrap_key を導出して MK を再 wrap し、新 login material
// と一緒に /auth/login/change-password へ送る。recovery_seed / passkey 鍵は MK 不変なので
// そのまま有効。
//
// 重い暗号は既存テスト済みモジュールに委譲し、本モジュールはオーケストレーションに
// 集中する。runChangePassword は依存注入可能で単体テスト可能。

import { b64encode } from "../crypto/b64.js";
import { deriveLoginMaterial } from "../crypto/login_kdf.js";
import { generateSalt } from "../crypto/argon2.js";
import { loadHashWasm } from "../crypto/hash_wasm_loader.js";
import { listWrappedKeys } from "../crypto/api.js";


function _zero(buf) {
  try { if (buf && buf.byteLength > 0) buf.fill(0); } catch (_e) { /* detached */ }
}


/**
 * ログインパスワードを変更する。
 *
 * 返り値:
 *   {status:"ok"}
 *   {status:"wrong_password"}   旧パスワード誤り (unwrap 失敗 or サーバ 401)
 *   {status:"error", message}   その他
 *
 * @param {Object} o
 * @param {string} o.oldPassword
 * @param {string} o.newPassword
 * @param {Object} o.client            SharedCryptoClient
 * @param {Object} [o.deps]            テスト用 DI
 */
export async function runChangePassword({ oldPassword, newPassword, client, deps = {} }) {
  const f = deps.fetchImpl ?? globalThis.fetch;
  const _loadHashWasm = deps.loadHashWasm ?? loadHashWasm;
  const _derive = deps.deriveLoginMaterial ?? deriveLoginMaterial;
  const _listWrappedKeys = deps.listWrappedKeys ?? listWrappedKeys;
  const _generateSalt = deps.generateSalt ?? generateSalt;

  if (!newPassword || newPassword.length < 8) {
    return { status: "error", message: "新しいパスワードは 8 文字以上にしてください。" };
  }

  await _loadHashWasm();

  // 現在の passphrase wrapped_key (旧 salt / kdf_params / 暗号文) を取得。
  const keys = await _listWrappedKeys();
  const pp = keys.find((k) => k.method === "passphrase");
  if (!pp) {
    return { status: "error", message: "パスワード鍵が見つかりません。" };
  }

  // 旧パスワードで mk_wrap_key を導出し MK を unwrap (= 旧パスワード検証)。
  const oldMat = await _derive(oldPassword, pp.salt, { params: pp.kdf_params });
  try {
    try {
      await client.unwrap(oldMat.mkWrapKey, pp.wrapped_master_key, pp.wrap_iv);
    } catch (_e) {
      // unwrap 失敗 = 旧パスワード誤り (GCM タグ検証 NG)
      return { status: "wrong_password" };
    }

    // 新 salt で新 material を導出し、MK を再 wrap。
    const newSalt = _generateSalt();
    const newMat = await _derive(newPassword, newSalt, {});
    // client.wrap は newMat.mkWrapKey を Transferable detach し、MK を再 wrap する。
    const wrapped = await client.wrap(newMat.mkWrapKey); // {wrapped, iv}
    let resp;
    try {
      resp = await f("/auth/login/change-password", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          old_login_verifier: b64encode(oldMat.loginVerifier),
          login_verifier: b64encode(newMat.loginVerifier),
          login_salt: b64encode(newSalt),
          login_kdf_params: { memory: 65536, iterations: 3, parallelism: 1 },
          wrapped_master_key: b64encode(wrapped.wrapped),
          wrap_iv: b64encode(wrapped.iv),
        }),
      });
    } finally {
      _zero(newSalt);
      _zero(newMat.loginVerifier);
      _zero(newMat.mkWrapKey);
    }
    if (resp.status === 401) {
      return { status: "wrong_password" };
    }
    if (!resp.ok) {
      return { status: "error", message: "パスワードの変更に失敗しました。" };
    }
    return { status: "ok" };
  } finally {
    _zero(oldMat.loginVerifier);
    _zero(oldMat.mkWrapKey);
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
}


function _bind() {
  const form = document.getElementById("change-password-form");
  if (!form) return;
  const statusEl = document.getElementById("change-password-status");

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const oldEl = form.querySelector('[name="current_password"]');
    const newEl = form.querySelector('[name="new_password"]');
    const confirmEl = form.querySelector('[name="new_password_confirm"]');
    const oldPassword = oldEl?.value || "";
    const newPassword = newEl?.value || "";
    if (!oldPassword || !newPassword) {
      _setMsg(statusEl, "error", "すべての項目を入力してください。");
      return;
    }
    if (newPassword !== (confirmEl?.value || "")) {
      _setMsg(statusEl, "error", "新しいパスワード (確認) が一致しません。");
      return;
    }
    const btn = form.querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    _setMsg(statusEl, "info", "変更しています…（鍵を再ラップ中）");

    const { SharedCryptoClient } = await import("../crypto/shared-client.js");
    const workerUrl = globalThis.IIKANJI_SHARED_WORKER_URL
      || "/static/js/crypto/shared-worker.js";
    const client = new SharedCryptoClient(workerUrl);
    let result;
    try {
      result = await runChangePassword({ oldPassword, newPassword, client });
    } catch (e) {
      console.error("change password error", e);
      result = { status: "error", message: "パスワードの変更に失敗しました。" };
    } finally {
      // パスワード入力欄をクリア
      [oldEl, newEl, confirmEl].forEach((el) => { if (el) el.value = ""; });
      try { client.close(); } catch (_e) { /* ignore */ }
    }
    if (result.status === "ok") {
      _setMsg(statusEl, "ok", "パスワードを変更しました。");
    } else if (result.status === "wrong_password") {
      _setMsg(statusEl, "error", "現在のパスワードが正しくありません。");
    } else {
      _setMsg(statusEl, "error", result.message || "パスワードの変更に失敗しました。");
    }
    if (btn) btn.disabled = false;
  });
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bind);
  } else {
    _bind();
  }
}
