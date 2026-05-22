// Master Key を Worker クロージャ内のみに保持する。window.* には絶対露出させない。
// メッセージスキーマは type ごとに固定し、不正型は { ok: false, error: ... } で返す。

let masterKey = null;

function isUint8(v) {
  return v instanceof Uint8Array;
}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

async function importMasterKey(rawBytes) {
  if (!isUint8(rawBytes) || rawBytes.byteLength !== 32) {
    throw new Error("master key must be Uint8Array of 32 bytes");
  }
  const key = await crypto.subtle.importKey(
    "raw",
    rawBytes,
    { name: "AES-GCM" },
    false,
    ["encrypt", "decrypt"],
  );
  // import 後の生バイト列は不要。GC 前にゼロ埋めしてメモリ残留を最小化する。
  rawBytes.fill(0);
  return key;
}

async function encrypt(plaintext, aad) {
  if (masterKey === null) throw new Error("key not set");
  if (!isUint8(plaintext)) throw new Error("plaintext must be Uint8Array");
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const params = { name: "AES-GCM", iv };
  if (aad !== undefined) {
    if (!isUint8(aad)) throw new Error("aad must be Uint8Array");
    params.additionalData = aad;
  }
  const ct = await crypto.subtle.encrypt(params, masterKey, plaintext);
  return { iv, ciphertext: new Uint8Array(ct) };
}

async function decrypt(ciphertext, iv, aad) {
  if (masterKey === null) throw new Error("key not set");
  if (!isUint8(ciphertext)) throw new Error("ciphertext must be Uint8Array");
  if (!isUint8(iv) || iv.byteLength !== 12) {
    throw new Error("iv must be Uint8Array of 12 bytes");
  }
  const params = { name: "AES-GCM", iv };
  if (aad !== undefined) {
    if (!isUint8(aad)) throw new Error("aad must be Uint8Array");
    params.additionalData = aad;
  }
  const pt = await crypto.subtle.decrypt(params, masterKey, ciphertext);
  return new Uint8Array(pt);
}

// 不正型を弾く。Q12 の対応: postMessage で送られてきた任意オブジェクトを盲信しない。
async function handle(msg) {
  if (!isPlainObject(msg) || typeof msg.type !== "string" || typeof msg.id !== "number") {
    throw new Error("invalid message shape");
  }
  switch (msg.type) {
    case "setKey": {
      masterKey = await importMasterKey(msg.rawKey);
      return { ok: true };
    }
    case "clearKey": {
      masterKey = null;
      return { ok: true };
    }
    case "encrypt": {
      const r = await encrypt(msg.plaintext, msg.aad);
      return { ok: true, iv: r.iv, ciphertext: r.ciphertext };
    }
    case "decrypt": {
      const pt = await decrypt(msg.ciphertext, msg.iv, msg.aad);
      return { ok: true, plaintext: pt };
    }
    case "generateKey": {
      // Worker 内で生成して Worker 内で import。rawKey はメインスレッドに返さない。
      const raw = crypto.getRandomValues(new Uint8Array(32));
      masterKey = await importMasterKey(raw);
      return { ok: true, keyBits: 256 };
    }
    default:
      throw new Error(`unknown type: ${msg.type}`);
  }
}

self.onmessage = async (ev) => {
  const msg = ev.data;
  const id = isPlainObject(msg) && typeof msg.id === "number" ? msg.id : -1;
  try {
    const result = await handle(msg);
    self.postMessage({ id, ...result });
  } catch (e) {
    self.postMessage({ id, ok: false, error: String(e && e.message || e) });
  }
};
