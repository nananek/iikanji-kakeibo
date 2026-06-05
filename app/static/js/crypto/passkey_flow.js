// WebAuthn PRF 認証フローのクライアントラッパー (E1 PR-F3a)。
//
// 設定画面の鍵設定ウィザードから呼ばれる高レベル API:
//
//   const result = await beginPasskeyKeyDerivation({ credentialId: 5 });
//   // result = { derivedKey: Uint8Array(32), credentialDbId: 5 }
//
// フロー:
//   1. POST /webauthn/key-derivation/options → challenge + allowCredentials
//   2. navigator.credentials.get(options) with PRF 拡張
//   3. credential.getClientExtensionResults().prf.results.first → 32B PRF 出力
//   4. POST /webauthn/key-derivation/finalize → 所有権検証 + sign_count 更新
//      → { credential_id: <db PK> }
//   5. HKDF-SHA256 で PRF 出力 → 32B derived_key
//
// セキュリティ:
//   - PRF 出力はサーバに送らない (authenticator → client のみ)
//   - サーバは challenge + 本人 credential 所有確認のみ
//   - derived_key は呼び出し側で wrap 後ゼロ埋め

import {
  buildPrfExtensionInput,
  deriveKeyFromCredential,
} from "./webauthn_prf.js";


function b64urlToBytes(b64url) {
  const b64 = b64url.replace(/-/g, "+").replace(/_/g, "/");
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const bin = atob(b64 + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToB64url(bytes) {
  let s = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}


/**
 * サーバから WebAuthn 認証オプションを取得し、PRF 拡張入力を追加して
 * `publicKey` 形式に整形する。
 * @param {number|null} credentialDbId — 特定 Passkey を使う場合の DB PK
 */
async function _fetchOptions(credentialDbId) {
  const r = await fetch("/webauthn/key-derivation/options", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
    },
    body: JSON.stringify(
      credentialDbId ? { credential_id: credentialDbId } : {},
    ),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`options 取得失敗: ${err.error || `HTTP ${r.status}`}`);
  }
  const opts = await r.json();
  // py_webauthn の options_to_json は base64url 文字列を返す
  // (challenge / allowCredentials.id)。これを Uint8Array に戻す + PRF 拡張を追加
  return {
    publicKey: {
      challenge: b64urlToBytes(opts.challenge),
      rpId: opts.rpId,
      timeout: opts.timeout,
      userVerification: opts.userVerification,
      allowCredentials: (opts.allowCredentials || []).map((c) => ({
        id: b64urlToBytes(c.id),
        type: c.type,
        transports: c.transports,
      })),
      extensions: buildPrfExtensionInput(),
    },
  };
}


/**
 * navigator.credentials.get のレスポンスを finalize エンドポイント
 * 用の JSON に整形する。
 *
 * 重要: PRF 出力 (results.first) はサーバに送信しない。`ArrayBuffer` は
 * `JSON.stringify` で `{}` になる暗黙挙動に依存するのは危険なので、
 * 明示的に `prf.results` キー全体を削除して "PRF 拡張が有効化されている"
 * という事実のみを伝える。
 */
function _credentialToJson(credential) {
  const resp = credential.response;
  const rawExt = credential.getClientExtensionResults
    ? credential.getClientExtensionResults()
    : {};
  // PRF 出力をサーバに漏らさないよう明示的にコピー + 削除
  const safeExt = { ...rawExt };
  if (safeExt.prf && typeof safeExt.prf === "object") {
    // results.first / results.second 等を一切送らない
    safeExt.prf = { enabled: true };
  }
  return {
    id: credential.id,
    rawId: bytesToB64url(new Uint8Array(credential.rawId)),
    type: credential.type,
    response: {
      authenticatorData: bytesToB64url(new Uint8Array(resp.authenticatorData)),
      clientDataJSON: bytesToB64url(new Uint8Array(resp.clientDataJSON)),
      signature: bytesToB64url(new Uint8Array(resp.signature)),
      userHandle: resp.userHandle
        ? bytesToB64url(new Uint8Array(resp.userHandle))
        : null,
    },
    clientExtensionResults: safeExt,
  };
}


async function _finalize(credentialJson) {
  const r = await fetch("/webauthn/key-derivation/finalize", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfToken(),
    },
    body: JSON.stringify(credentialJson),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`finalize 失敗: ${err.error || `HTTP ${r.status}`}`);
  }
  return r.json(); // { ok, credential_id }
}


/**
 * Passkey PRF 鍵派生のメインエントリポイント。
 *
 * @param {Object} [opts]
 * @param {number} [opts.credentialId] — 特定 Passkey に限定する DB PK
 * @returns {Promise<{ derivedKey: Uint8Array, credentialDbId: number }>}
 * @throws {Error} ユーザーキャンセル・PRF 非対応・通信失敗
 */
export async function beginPasskeyKeyDerivation(opts = {}) {
  if (typeof navigator?.credentials?.get !== "function") {
    throw new Error("WebAuthn 非対応のブラウザです");
  }
  const options = await _fetchOptions(opts.credentialId ?? null);
  let credential;
  try {
    credential = await navigator.credentials.get(options);
  } catch (e) {
    throw new Error(
      `Passkey 認証がキャンセルまたは失敗しました: ${e?.message || e}`,
    );
  }
  if (!credential) {
    throw new Error("Passkey 認証がキャンセルされました");
  }
  const derivedKey = await deriveKeyFromCredential(credential);
  if (!derivedKey) {
    // PRF は「端末」ではなく**パスキーの保存先(認証情報基盤)**が対応している
    // 必要がある。Bitwarden 等の一部パスキー管理ツールは PRF 拡張を返さない
    // ため、ここで null になる。端末のせいに聞こえない文言にする。
    throw new Error(
      "お使いのパスキーの保存先が暗号鍵生成 (WebAuthn PRF 拡張) に対応していません。"
      + "Bitwarden など一部のパスキー管理ツールは未対応です。"
      + "パスフレーズ方式をご利用いただくか、PRF 対応のパスキー "
      + "(iCloud キーチェーン / Windows Hello / ハードウェアキー等) をご利用ください。",
    );
  }
  // PRF 出力検証ができたので finalize で sign_count 更新 + 所有権確認
  const credentialJson = _credentialToJson(credential);
  const finalizeResult = await _finalize(credentialJson);
  return {
    derivedKey,
    credentialDbId: finalizeResult.credential_id,
  };
}
