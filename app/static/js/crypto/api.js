// /api/v1/wrapped-keys クライアントヘルパー (E1 #108)。
//
// 暗号文 (Uint8Array) と base64 文字列の変換、CSRF トークン取得を含む。
// 設計書 §10.3 の API 仕様参照。

function _csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

function _baseHeaders() {
  return {
    "Content-Type": "application/json",
    "X-CSRFToken": _csrfToken(),
  };
}

export function b64encode(bytes) {
  let s = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s);
}

export function b64decode(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** 自身の wrapped_keys 一覧を取得。各エントリの暗号文は Uint8Array に変換済。 */
export async function listWrappedKeys() {
  const r = await fetch("/api/v1/wrapped-keys", {
    credentials: "include",
  });
  if (!r.ok) throw new Error(`list failed: HTTP ${r.status}`);
  const data = await r.json();
  return (data.wrapped_keys || []).map((row) => ({
    id: row.id,
    method: row.method,
    webauthn_credential_id: row.webauthn_credential_id,
    wrapped_master_key: b64decode(row.wrapped_master_key),
    wrap_iv: b64decode(row.wrap_iv),
    salt: row.salt ? b64decode(row.salt) : null,
    kdf_params: row.kdf_params,
    label: row.label,
    created_at: row.created_at,
    last_used_at: row.last_used_at,
  }));
}

/**
 * 新規 wrapped MK を登録。
 *
 * payload:
 *   method: "passkey_prf" | "passphrase" | "recovery_seed"
 *   wrapped_master_key: Uint8Array
 *   wrap_iv: Uint8Array (12B)
 *   salt: Uint8Array (16B, passphrase 時必須) | null
 *   kdf_params: { memory, iterations, parallelism } | null
 *   webauthn_credential_id: number | null
 *   label: string | null
 * 追加オプション:
 *   rotationToken: string (X-Rotation-Id ヘッダ、ローテーション中の create)
 */
export async function createWrappedKey(payload, opts = {}) {
  const body = {
    method: payload.method,
    wrapped_master_key: b64encode(payload.wrapped_master_key),
    wrap_iv: b64encode(payload.wrap_iv),
    salt: payload.salt ? b64encode(payload.salt) : null,
    kdf_params: payload.kdf_params ?? null,
    webauthn_credential_id: payload.webauthn_credential_id ?? null,
    label: payload.label ?? null,
  };
  const headers = _baseHeaders();
  if (opts.rotationToken) headers["X-Rotation-Id"] = opts.rotationToken;
  const r = await fetch("/api/v1/wrapped-keys", {
    method: "POST",
    credentials: "include",
    headers,
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`create failed: HTTP ${r.status} ${err.error || ""}`);
  }
  return r.json();
}

export async function deleteWrappedKey(id) {
  const r = await fetch(`/api/v1/wrapped-keys/${id}`, {
    method: "DELETE",
    credentials: "include",
    headers: { "X-CSRFToken": _csrfToken() },
  });
  if (r.status === 204) return;
  if (r.status === 409) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`cannot delete last key: ${err.error || ""}`);
  }
  throw new Error(`delete failed: HTTP ${r.status}`);
}

export async function touchWrappedKey(id) {
  const r = await fetch(`/api/v1/wrapped-keys/${id}/touch`, {
    method: "PUT",
    credentials: "include",
    headers: { "X-CSRFToken": _csrfToken() },
  });
  if (!r.ok) throw new Error(`touch failed: HTTP ${r.status}`);
  return r.json();
}

// --- ローテーション ---

export async function rotateBegin() {
  const r = await fetch("/api/v1/wrapped-keys/rotate/begin", {
    method: "POST",
    credentials: "include",
    headers: _baseHeaders(),
  });
  if (!r.ok) throw new Error(`rotate begin failed: HTTP ${r.status}`);
  return r.json();  // { rotation_token, auto_abort_at }
}

export async function rotateCommit(rotationToken) {
  const r = await fetch("/api/v1/wrapped-keys/rotate/commit", {
    method: "POST",
    credentials: "include",
    headers: { ..._baseHeaders(), "X-Rotation-Id": rotationToken },
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`rotate commit failed: HTTP ${r.status} ${err.error || ""}`);
  }
  return r.json();  // { deleted }
}

export async function rotateAbort(rotationToken) {
  const r = await fetch("/api/v1/wrapped-keys/rotate/abort", {
    method: "POST",
    credentials: "include",
    headers: { ..._baseHeaders(), "X-Rotation-Id": rotationToken },
  });
  if (!r.ok) throw new Error(`rotate abort failed: HTTP ${r.status}`);
  return r.json();  // { deleted }
}
