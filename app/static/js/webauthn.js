/**
 * WebAuthn / Passkey ヘルパー
 */

function base64urlToBuffer(base64url) {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/");
  const pad = base64.length % 4 === 0 ? "" : "=".repeat(4 - (base64.length % 4));
  const binary = atob(base64 + pad);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes.buffer;
}

function bufferToBase64url(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Passkey 登録
 * @param {string} passkeyName - ユーザーが付けたPasskeyの名前
 * @returns {Promise<object>}
 */
async function registerPasskey(passkeyName) {
  // 1. サーバーからオプション取得
  const optionsResp = await fetch("/webauthn/register/options", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!optionsResp.ok) {
    throw new Error("登録オプションの取得に失敗しました。");
  }
  const options = await optionsResp.json();

  // ArrayBuffer に変換
  options.challenge = base64urlToBuffer(options.challenge);
  options.user.id = base64urlToBuffer(options.user.id);
  if (options.excludeCredentials) {
    options.excludeCredentials = options.excludeCredentials.map((c) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    }));
  }

  // 2. ブラウザの WebAuthn API 呼び出し
  const credential = await navigator.credentials.create({ publicKey: options });

  // 3. サーバーに送って検証
  const body = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      attestationObject: bufferToBase64url(
        credential.response.attestationObject
      ),
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      transports: credential.response.getTransports
        ? credential.response.getTransports()
        : [],
    },
    passkey_name: passkeyName || "",
  };

  const verifyResp = await fetch("/webauthn/register/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await verifyResp.json();
}

/**
 * Passkey 認証（ログイン）
 * @returns {Promise<object>}
 */
async function authenticatePasskey() {
  // 1. サーバーからオプション取得
  const optionsResp = await fetch("/webauthn/authenticate/options", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!optionsResp.ok) {
    throw new Error("認証オプションの取得に失敗しました。");
  }
  const options = await optionsResp.json();

  // ArrayBuffer に変換
  options.challenge = base64urlToBuffer(options.challenge);
  if (options.allowCredentials) {
    options.allowCredentials = options.allowCredentials.map((c) => ({
      ...c,
      id: base64urlToBuffer(c.id),
    }));
  }

  // 2. ブラウザの WebAuthn API 呼び出し
  const credential = await navigator.credentials.get({ publicKey: options });

  // 3. サーバーに送って検証
  const body = {
    id: credential.id,
    rawId: bufferToBase64url(credential.rawId),
    type: credential.type,
    response: {
      authenticatorData: bufferToBase64url(
        credential.response.authenticatorData
      ),
      clientDataJSON: bufferToBase64url(credential.response.clientDataJSON),
      signature: bufferToBase64url(credential.response.signature),
      userHandle: credential.response.userHandle
        ? bufferToBase64url(credential.response.userHandle)
        : null,
    },
  };

  const verifyResp = await fetch("/webauthn/authenticate/verify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await verifyResp.json();
}
