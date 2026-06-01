// E2EE X25519 鍵ペア生成・保管 helper (E5 #112 PR-A / 設計書 §14)。
//
// 監査連携で owner / auditor が相手の公開鍵宛にスナップショットを HPKE 暗号化
// するための長期鍵ペアを用意する。秘密鍵は MK で AES-GCM 暗号化 (SharedWorker
// 内で完結) してからサーバに保管し、平文秘密鍵はメインスレッドに残さない。
//
// 公開鍵: raw 32B を平文で users.public_key に保管
// 秘密鍵: pkcs8 (48B) を MK で暗号化した暗号文 + IV を保管
//
// 鍵ペアは初回 MK 設定時に生成し、既存 MK ユーザーは MK 解錠時に lazy backfill
// する (public_key 未設定なら生成→保管)。回転は監査パッケージ再暗号化を伴うため
// E5 後続スコープで、ここでは「未設定なら 1 度だけ生成」のみ扱う。

import { b64encode, b64decode } from "./b64.js";
import { uint64BE } from "./record.js";

const TEXT_ENC = new TextEncoder();
const NUL = TEXT_ENC.encode("\0");

// 秘密鍵ラップ用 AAD: "x25519-priv" \0 uint64BE(userId)。record.js の AAD と
// 同思想で、暗号文を当該ユーザーに束縛する (他ユーザーの blob すり替えを検知)。
// HPKE open (shared-client.hpkeOpen) 側で秘密鍵を復号する際に同じ AAD が要るため
// export する。
export function privateKeyAAD(userId) {
  const prefix = TEXT_ENC.encode("x25519-priv");
  const uid = uint64BE(userId);
  const out = new Uint8Array(prefix.length + NUL.length + uid.length);
  out.set(prefix, 0);
  out.set(NUL, prefix.length);
  out.set(uid, prefix.length + NUL.length);
  return out;
}

function _csrfToken() {
  const meta =
    typeof document !== "undefined"
      ? document.querySelector('meta[name="csrf-token"]')
      : null;
  return meta ? meta.getAttribute("content") : "";
}

/**
 * X25519 鍵ペアを WebCrypto で生成する。
 * @returns {Promise<{publicRaw: Uint8Array, privatePkcs8: Uint8Array}>}
 *   publicRaw = 32B raw 公開鍵, privatePkcs8 = pkcs8 形式の秘密鍵 (機密)
 * @throws WebCrypto が X25519 未対応の環境では例外。
 */
export async function generateX25519KeyPair() {
  const kp = await crypto.subtle.generateKey(
    { name: "X25519" },
    /* extractable */ true,
    ["deriveBits"],
  );
  const publicRaw = new Uint8Array(
    await crypto.subtle.exportKey("raw", kp.publicKey),
  );
  const privatePkcs8 = new Uint8Array(
    await crypto.subtle.exportKey("pkcs8", kp.privateKey),
  );
  return { publicRaw, privatePkcs8 };
}

/**
 * サーバから自身の鍵ペアを取得。各フィールドは Uint8Array または null。
 * @param {typeof fetch} [fetchImpl]
 */
export async function getKeyPair(fetchImpl = fetch) {
  const r = await fetchImpl("/api/v1/keypair", { credentials: "include" });
  if (!r.ok) throw new Error(`keypair get failed: HTTP ${r.status}`);
  const data = await r.json();
  return {
    public_key: data.public_key ? b64decode(data.public_key) : null,
    encrypted_private_key: data.encrypted_private_key
      ? b64decode(data.encrypted_private_key)
      : null,
    private_key_iv: data.private_key_iv ? b64decode(data.private_key_iv) : null,
  };
}

/**
 * 鍵ペアをサーバに保存。public_key 設定済みなら 409。
 * @param {{publicRaw: Uint8Array, encBlob: Uint8Array, iv: Uint8Array}} payload
 * @param {typeof fetch} [fetchImpl]
 */
export async function putKeyPair({ publicRaw, encBlob, iv }, fetchImpl = fetch) {
  const r = await fetchImpl("/api/v1/keypair", {
    method: "PUT",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": _csrfToken(),
    },
    body: JSON.stringify({
      public_key: b64encode(publicRaw),
      encrypted_private_key: b64encode(encBlob),
      private_key_iv: b64encode(iv),
    }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(`keypair put failed: HTTP ${r.status} ${err.error || ""}`);
  }
  return r.json();
}

/**
 * 鍵ペアが未設定なら生成し、秘密鍵を MK でラップして保管する (lazy backfill)。
 *
 * @param {Object} client  SharedCryptoClient (encrypt(plaintext, aad) を提供)
 * @param {number|bigint} userId  AAD 束縛用ユーザー ID
 * @param {typeof fetch} [fetchImpl]
 * @returns {Promise<boolean>}  生成・保管したら true、既存なら false
 */
export async function ensureKeyPair(client, userId, fetchImpl = fetch) {
  if (!client || typeof client.encrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (userId === undefined || userId === null) {
    throw new Error("userId is required");
  }
  const existing = await getKeyPair(fetchImpl);
  if (existing.public_key) {
    return false; // 既に鍵ペアあり (回転は E5 後続スコープ)
  }

  const { publicRaw, privatePkcs8 } = await generateX25519KeyPair();
  try {
    const aad = privateKeyAAD(userId);
    // SharedWorker 内の MK で AES-GCM 暗号化。MK はメインスレッドに出ない。
    const { ciphertext, iv } = await client.encrypt(privatePkcs8, aad);
    await putKeyPair({ publicRaw, encBlob: ciphertext, iv }, fetchImpl);
    return true;
  } finally {
    // 平文秘密鍵 (pkcs8) はメインスレッドに残さずゼロ埋め。
    try {
      privatePkcs8.fill(0);
    } catch (_e) {
      /* detached */
    }
  }
}
