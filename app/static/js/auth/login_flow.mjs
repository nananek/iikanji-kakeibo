// #385 PR-3a: ログイン画面の 2 ラウンドログイン + v4 透過移行ドライバ。
//
// 設計書 docs/v5-e2ee/login-derived-mk.md §3.2 / §3.5。
//
// ログインパスワードから master を Argon2id で導出し、HKDF split した login_verifier
// でサーバ認証、mk_wrap_key で MK を unwrap/wrap する。v4 ユーザーは初回ログイン時に
// 透過移行する: MK 生成 → wrapped_key 確立 → 鍵ペア生成 (ゲート解除) → temp-MK→自 MK
// の rewrap を進捗バー付きで駆動 → finalize。
//
// 重い暗号 (Argon2id / HKDF / rewrap) は既存テスト済みモジュールに委譲し、本モジュールは
// オーケストレーションに集中する。`runLoginFlow` は依存を注入可能にして単体テスト可能。

import { b64decode, b64encode } from "../crypto/b64.js";
import { deriveLoginMaterial } from "../crypto/login_kdf.js";
import { loadHashWasm } from "../crypto/hash_wasm_loader.js";
import { ensureKeyPair } from "../crypto/keypair.js";
import { listWrappedKeys } from "../crypto/api.js";
import { runRewrapMigration } from "../migration/rewrap_flow.js";
import { SharedCryptoClient } from "../crypto/shared-client.js";


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function _zero(buf) {
  try {
    if (buf && buf.byteLength > 0) buf.fill(0);
  } catch (_e) {
    /* Transferable で detach 済みなら fill は throw する。無視。 */
  }
}


async function _postJson(fetchImpl, url, body) {
  return fetchImpl(url, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}


/**
 * 2 ラウンドログイン + (必要なら) v4 透過移行を実行する。
 *
 * 返り値 (DOM には触れない。呼び出し側が解釈する):
 *   {status:"redirect", url}      ログイン成功 (MK 解錠済)
 *   {status:"fallback"}           LOGIN_SERVER_SECRET 未設定 (503)。従来フォーム送信へ
 *   {status:"password_setup"}     passkey_only 等パスワード未保有。パスキーログインへ誘導
 *   {status:"error", message}     失敗 (message は固定文言のみ。サーバ応答は載せない)
 *
 * @param {Object} o
 * @param {string} o.username
 * @param {string} o.password
 * @param {string} [o.nextUrl]            成功時のリダイレクト先 (検証済みの内部パス)
 * @param {Object} o.client              SharedCryptoClient
 * @param {Function} [o.onStatus]        (text) 状況テキスト更新
 * @param {Function} [o.onProgress]      (done, total) 進捗更新
 * @param {Object} [o.deps]              テスト用 DI (fetchImpl / 各暗号関数)
 */
export async function runLoginFlow({
  username, password, nextUrl = "/", client,
  onStatus = () => {}, onProgress = () => {}, deps = {},
}) {
  const f = deps.fetchImpl ?? globalThis.fetch;
  const _loadHashWasm = deps.loadHashWasm ?? loadHashWasm;
  const _derive = deps.deriveLoginMaterial ?? deriveLoginMaterial;
  const _ensureKeyPair = deps.ensureKeyPair ?? ensureKeyPair;
  const _listWrappedKeys = deps.listWrappedKeys ?? listWrappedKeys;
  const _runRewrap = deps.runRewrapMigration ?? runRewrapMigration;

  // 1) begin: salt / kdf_params / migration_required を得る。
  const beginResp = await _postJson(f, "/auth/login/begin", { username });
  if (beginResp.status === 503) {
    return { status: "fallback" };
  }
  if (!beginResp.ok) {
    return { status: "error", message: "ユーザー名またはパスワードが正しくありません。" };
  }
  const begin = await beginResp.json();
  if (begin.requires_password_setup) {
    return { status: "password_setup" };
  }

  // 2) master = Argon2id(password, salt) → HKDF split。
  onStatus("鍵を解錠しています…");
  await _loadHashWasm();
  const salt = b64decode(begin.salt);
  let material;
  try {
    material = await _derive(password, salt, { params: begin.kdf_params });
  } catch (_e) {
    return { status: "error", message: "鍵の導出に失敗しました。" };
  }

  try {
    if (begin.migration_required) {
      return await _doMigrate({
        f, client, username, password, begin, material,
        nextUrl, onStatus, onProgress,
        ensureKeyPairImpl: _ensureKeyPair, runRewrapImpl: _runRewrap,
      });
    }
    return await _doNormal({
      f, client, username, material, nextUrl,
      listWrappedKeysImpl: _listWrappedKeys,
    });
  } finally {
    // login_verifier はゼロ化。mk_wrap_key は wrap/unwrap で Transferable detach 済。
    _zero(material.loginVerifier);
    _zero(material.mkWrapKey);
  }
}


async function _doNormal({ f, client, username, material, nextUrl, listWrappedKeysImpl }) {
  const resp = await _postJson(f, "/auth/login/finish", {
    username,
    login_verifier: b64encode(material.loginVerifier),
  });
  if (!resp.ok) {
    return { status: "error", message: "ユーザー名またはパスワードが正しくありません。" };
  }
  // 認証成功 (セッション確立)。passphrase wrapped_key を取得して MK を unwrap する。
  const keys = await listWrappedKeysImpl();
  const pp = keys.find((k) => k.method === "passphrase");
  if (!pp) {
    return {
      status: "error",
      message: "暗号鍵が見つかりません。設定から鍵を再設定してください。",
    };
  }
  // unwrap は mk_wrap_key を Transferable detach し、MK を SharedWorker に展開する。
  await client.unwrap(material.mkWrapKey, pp.wrapped_master_key, pp.wrap_iv);
  return { status: "redirect", url: nextUrl };
}


async function _doMigrate({
  f, client, username, password, begin, material, nextUrl,
  onStatus, onProgress, ensureKeyPairImpl, runRewrapImpl,
}) {
  // 本人専用の新 MK を生成し、mk_wrap_key で wrap する。
  await client.generateKey();
  let wrapped;
  try {
    wrapped = await client.wrap(material.mkWrapKey); // {wrapped, iv}。mk_wrap_key detach。
  } catch (_e) {
    await client.clearKey();
    return { status: "error", message: "鍵の作成に失敗しました。" };
  }

  const resp = await _postJson(f, "/auth/login/finish", {
    username,
    password,
    login_verifier: b64encode(material.loginVerifier),
    login_salt: begin.salt,
    login_kdf_params: begin.kdf_params,
    wrapped_master_key: b64encode(wrapped.wrapped),
    wrap_iv: b64encode(wrapped.iv),
  });
  if (!resp.ok) {
    // 失敗 → Worker の MK を破棄して矛盾状態を残さない。
    await client.clearKey();
    if (resp.status === 401) {
      return { status: "error", message: "ユーザー名またはパスワードが正しくありません。" };
    }
    return { status: "error", message: "移行に失敗しました。時間をおいて再度お試しください。" };
  }
  const fin = await resp.json();

  // 鍵ペア (X25519) を生成・保管する。public_key が立つと鍵未設定ゲートが自己回復する。
  onStatus("暗号鍵を準備しています…");
  await ensureKeyPairImpl(client, fin.user_id, f);

  // temp-MK→自 MK の rewrap を進捗バー付きで駆動し finalize する。
  if (fin.needs_rewrap) {
    onStatus("データを暗号化しています…");
    await runRewrapImpl({
      client, userId: fin.user_id, years: fin.years || [],
      fetchImpl: f, onProgress, onStatus,
    });
  }
  return { status: "redirect", url: nextUrl };
}


// ------------------------------------------------------------------
// DOM 配線 (ブラウザのみ)
// ------------------------------------------------------------------

/** location.search の next を内部パスに限り採用する (オープンリダイレクト防止)。 */
export function safeNextUrl(search) {
  try {
    const next = new URLSearchParams(search || "").get("next") || "";
    // '/' 始まり かつ '//' (プロトコル相対) でない内部パスのみ許可。
    if (/^\/(?!\/)/.test(next)) return next;
  } catch (_e) {
    /* ignore */
  }
  return "/";
}


function _setStatus(el, kind, text) {
  if (!el) return;
  const icon = {
    spinner: '<i class="bi bi-hourglass-split"></i>',
    success: '<i class="bi bi-check-circle"></i>',
    error: '<i class="bi bi-x-circle"></i>',
    warn: '<i class="bi bi-exclamation-triangle"></i>',
  }[kind] || "";
  const cls = {
    spinner: "text-info", success: "text-success",
    error: "text-danger", warn: "text-warning",
  }[kind] || "";
  el.classList.remove("d-none");
  // text は固定文言のみ (サーバ応答を載せない)。アイコンは定数 HTML。
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = " " + text;
  el.innerHTML = icon;
  el.appendChild(span);
}


function _setProgress(wrap, bar, done, total) {
  if (wrap) wrap.classList.remove("d-none");
  if (!bar) return;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  bar.style.width = pct + "%";
  bar.setAttribute("aria-valuenow", String(pct));
  bar.textContent = pct + "%";
}


function _bind() {
  const form = document.getElementById("login-form");
  if (!form) return;
  const statusEl = document.getElementById("login-status");
  const progressWrap = document.getElementById("login-progress");
  const progressBar = document.getElementById("login-progress-bar");

  form.addEventListener("submit", async (ev) => {
    const usernameEl = form.querySelector('[name="username"]');
    const passwordEl = form.querySelector('[name="password"]');
    const username = (usernameEl?.value || "").trim();
    const password = passwordEl?.value || "";
    // 未入力はネイティブのバリデーション/送信に委ねる。
    if (!username || !password) return;

    ev.preventDefault();
    const submitBtn = form.querySelector('[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    _setStatus(statusEl, "spinner", "確認しています…");

    const client = new SharedCryptoClient(getSharedWorkerUrl());
    let result;
    try {
      result = await runLoginFlow({
        username, password,
        nextUrl: safeNextUrl(globalThis.location?.search),
        client,
        onStatus: (t) => _setStatus(statusEl, "spinner", t),
        onProgress: (d, t) => _setProgress(progressWrap, progressBar, d, t),
      });
    } catch (e) {
      console.error("login flow error", e);
      result = { status: "error", message: "ログインに失敗しました。時間をおいて再度お試しください。" };
    } finally {
      // パスワード文字列は JS ではゼロ化不能だが、入力欄からは消す。
      if (passwordEl) passwordEl.value = "";
    }

    if (result.status === "redirect") {
      _setStatus(statusEl, "success", "ログインしました。");
      globalThis.location.href = result.url;
      return; // SharedWorker の MK は次ページでも有効なので client.close しない。
    }
    try { client.close(); } catch (_e) { /* ignore */ }
    if (result.status === "fallback") {
      // LOGIN_SERVER_SECRET 未設定 → 従来の werkzeug フォーム送信にフォールバック。
      form.submit();
      return;
    }
    if (result.status === "password_setup") {
      _setStatus(statusEl, "warn", "このアカウントはパスキーでログインしてください。");
    } else {
      _setStatus(statusEl, "error", result.message || "ログインに失敗しました。");
    }
    if (submitBtn) submitBtn.disabled = false;
  });
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bind);
  } else {
    _bind();
  }
}
