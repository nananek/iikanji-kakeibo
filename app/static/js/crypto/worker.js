// E2EE Master Key 管理 Web Worker (E1 #108、設計書 §5 / §10.2-10.4)。
//
// Master Key (MK) を Worker クロージャ内のみに保持し、メインスレッドや
// window 名前空間には絶対に露出させない (XSS 1 件で全データ漏洩を防ぐ)。
// postMessage のメッセージは type ごとに型を検証し、不正型は
// { ok: false, error } で返す (Q12 対応)。
//
// 並行性に注意:
// - onmessage は async のため、wrap が `await` で yield 中に clearKey 等が
//   割り込みうる。wrap は await 前に rawMasterKey.slice() でスナップショット
//   を取って影響を遮断する
//
// エラーパスでも鍵素材は必ずゼロ埋め: 全 case で try/finally。

import {
  aesGcmDecrypt,
  aesGcmEncrypt,
  importAesKey,
  isPlainObject,
  isUint8,
  unwrapMasterKey,
  wrapMasterKey,
} from "./primitives.js";


let masterKey = null;      // CryptoKey (encrypt/decrypt 用)
let rawMasterKey = null;   // Uint8Array 32B (wrap 用)。Worker 内のみに保持

async function setMkFromRaw(rawBytes) {
  if (!isUint8(rawBytes) || rawBytes.byteLength !== 32) {
    throw new Error("master key must be Uint8Array of 32 bytes");
  }
  masterKey = await importAesKey(rawBytes, ["encrypt", "decrypt"]);
  if (rawMasterKey) rawMasterKey.fill(0);
  rawMasterKey = new Uint8Array(rawBytes); // wrap 用に raw を保持
}

async function handle(msg) {
  if (
    !isPlainObject(msg) ||
    typeof msg.type !== "string" ||
    typeof msg.id !== "number"
  ) {
    throw new Error("invalid message shape");
  }
  switch (msg.type) {
    case "generateKey": {
      const raw = crypto.getRandomValues(new Uint8Array(32));
      try {
        await setMkFromRaw(raw);
      } finally {
        raw.fill(0);
      }
      return { ok: true, keyBits: 256 };
    }
    case "setKey": {
      const raw = msg.rawKey;
      try {
        await setMkFromRaw(raw);
      } finally {
        if (isUint8(raw)) raw.fill(0);
      }
      return { ok: true };
    }
    case "clearKey": {
      masterKey = null;
      if (rawMasterKey) rawMasterKey.fill(0);
      rawMasterKey = null;
      return { ok: true };
    }
    case "encrypt": {
      if (masterKey === null) throw new Error("master key not set");
      const r = await aesGcmEncrypt(masterKey, msg.plaintext, msg.aad);
      return { ok: true, iv: r.iv, ciphertext: r.ciphertext };
    }
    case "decrypt": {
      if (masterKey === null) throw new Error("master key not set");
      const pt = await aesGcmDecrypt(
        masterKey, msg.ciphertext, msg.iv, msg.aad,
      );
      return { ok: true, plaintext: pt };
    }
    case "wrap": {
      if (rawMasterKey === null) throw new Error("master key not set");
      // await 前にスナップショットを取る。await 中に clearKey が割り込むと
      // rawMasterKey が all-zero になるため、wrap 対象を確定させておく。
      const snapshot = rawMasterKey.slice();
      try {
        const r = await wrapMasterKey(snapshot, msg.derivedKey);
        return { ok: true, iv: r.iv, wrapped: r.ciphertext };
      } finally {
        snapshot.fill(0);
        if (isUint8(msg.derivedKey)) msg.derivedKey.fill(0);
      }
    }
    case "unwrap": {
      let rawMk;
      try {
        rawMk = await unwrapMasterKey(
          msg.wrapped, msg.iv, msg.derivedKey,
        );
      } finally {
        // unwrap 失敗 (タグ検証 NG = パスフレーズ誤り等) は通常パス。
        // 失敗時でも derivedKey は確実にゼロ埋め。
        if (isUint8(msg.derivedKey)) msg.derivedKey.fill(0);
      }
      try {
        await setMkFromRaw(rawMk);
      } finally {
        rawMk.fill(0);
      }
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
    self.postMessage({ id, ok: false, error: String((e && e.message) || e) });
  }
};
