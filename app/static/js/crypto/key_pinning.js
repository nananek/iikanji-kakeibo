// TOFU (Trust On First Use) 公開鍵 pinning + fingerprint 計算 (E5 #112 / 設計書 §14.4)。
//
// 監査連携で owner は相手 (auditor) の X25519 公開鍵を初回に「指紋 (fingerprint)」
// として帯域外 (電話 / 対面 / 紙) で確認し、IndexedDB に pinning する。以降サーバが
// 返す公開鍵が pinning と異なれば中間者攻撃の可能性として警告する。サーバ側に
// 「確認済」フラグは置かない (置くと TOFU の意味がない) ので、すべてクライアント
// 側 (IndexedDB) で管理する。
//
// このモジュールは MK を一切必要としない。扱うのは公開情報 (公開鍵) のハッシュと
// pinning メタデータのみで、暗号化処理は持たない。
//
// 設計:
//   - fingerprint / base32 / 比較 などの純粋ロジックは IndexedDB 非依存で export し、
//     node --test で単体検証する。
//   - IndexedDB アクセスは薄いラッパー (openPinStore) に閉じ込め、store を引数注入
//     可能にして高レベル関数 (evaluatePin / pinKey) をブラウザ非依存にする。

// RFC 4648 Base32 アルファベット (大文字 + 2-7、紛らわしい 0/1/8/9 を含まない)。
const BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

const HEX = "0123456789abcdef";

/**
 * バイト列を RFC 4648 Base32 (パディング無し) でエンコードする。
 * @param {Uint8Array} bytes
 * @returns {string}
 */
export function base32Encode(bytes) {
  let bits = 0;
  let value = 0;
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    value = (value << 8) | bytes[i];
    bits += 8;
    while (bits >= 5) {
      out += BASE32_ALPHABET[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
  }
  if (bits > 0) {
    out += BASE32_ALPHABET[(value << (5 - bits)) & 31];
  }
  return out;
}

/**
 * バイト列を 16 進文字列に変換する。
 * @param {Uint8Array} bytes
 * @returns {string}
 */
export function bytesToHex(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += HEX[bytes[i] >>> 4] + HEX[bytes[i] & 15];
  }
  return out;
}

/**
 * 公開鍵の SHA-256 と、人間が帯域外確認するための fingerprint ラベルを計算する。
 *
 * ラベルは SHA-256(public_key) の先頭 20 バイト (160 bit) を Base32 化し、4 文字
 * ごとに "-" で区切って "iikanji-<ROLE>-XXXX-..." 形式にする (§14.4)。比較は衝突
 * 耐性のため SHA-256 全 32 バイトの hex で行い、ラベルは表示専用とする。
 *
 * @param {Uint8Array} publicKeyRaw  raw 32B の X25519 公開鍵
 * @param {string} [role="AUDITOR"]  ラベルに埋め込む相手の役割
 * @returns {Promise<{hashHex: string, label: string}>}
 */
export async function computeFingerprint(publicKeyRaw, role = "AUDITOR") {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", publicKeyRaw),
  );
  const hashHex = bytesToHex(digest);
  const b32 = base32Encode(digest.subarray(0, 20)); // 160 bit → 32 文字
  const groups = b32.match(/.{1,4}/g) || [];
  const label = `iikanji-${role}-${groups.join("-")}`;
  return { hashHex, label };
}

/**
 * pinning 済ハッシュと現在のハッシュを比較する。
 * @param {string} pinnedHashHex
 * @param {string} currentHashHex
 * @returns {"match"|"mismatch"}
 */
export function classifyPin(pinnedHashHex, currentHashHex) {
  return pinnedHashHex === currentHashHex ? "match" : "mismatch";
}

// ---- IndexedDB pinning ストア --------------------------------------------

const DB_VERSION = 1;
const STORE = "pinned_keys";

/**
 * pinning ストアの IndexedDB 名を owner ユーザー ID でスコープする。
 *
 * 同一ブラウザを複数の owner アカウントが共有しても、各 owner の TOFU 固定が
 * 混ざらないようにする (owner A が auditor X を固定した結果を owner B が「確認済」
 * と誤認する/「不一致」と誤警告するのを防ぐ)。
 *
 * @param {number|string} ownerUserId
 * @returns {string}
 */
export function pinStoreDbName(ownerUserId) {
  if (ownerUserId === undefined || ownerUserId === null || ownerUserId === "") {
    throw new Error("ownerUserId is required for pin store scoping");
  }
  return `iikanji-tofu-${ownerUserId}`;
}

function _openDb(dbName) {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(dbName, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "peer_user_id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function _txDone(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = tx.onerror = () => reject(tx.error);
  });
}

/**
 * IndexedDB を backend とする pinning ストアを開く (ブラウザ専用)。
 * ストアは owner ユーザー ID でスコープされる (pinStoreDbName 参照)。
 * 返り値は {get, put, delete} を持つ非同期ストア。
 * @param {number|string} ownerUserId  現在ログイン中の owner ユーザー ID
 * @returns {Promise<{get: Function, put: Function, delete: Function}>}
 */
export async function openPinStore(ownerUserId) {
  const db = await _openDb(pinStoreDbName(ownerUserId));
  return {
    async get(peerId) {
      const tx = db.transaction(STORE, "readonly");
      const req = tx.objectStore(STORE).get(Number(peerId));
      const val = await new Promise((resolve, reject) => {
        req.onsuccess = () => resolve(req.result || null);
        req.onerror = () => reject(req.error);
      });
      return val;
    },
    async put(record) {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put(record);
      await _txDone(tx);
    },
    async delete(peerId) {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(Number(peerId));
      await _txDone(tx);
    },
  };
}

/**
 * テスト / 非ブラウザ環境向けの in-memory pinning ストア。openPinStore と同じ
 * インターフェースを持つ。
 * @returns {{get: Function, put: Function, delete: Function}}
 */
export function createMemoryPinStore() {
  const map = new Map();
  return {
    async get(peerId) {
      return map.get(Number(peerId)) || null;
    },
    async put(record) {
      map.set(Number(record.peer_user_id), record);
    },
    async delete(peerId) {
      map.delete(Number(peerId));
    },
  };
}

/**
 * 相手の公開鍵の pinning 状態を評価する。
 *
 * 戻り値の status:
 *   - "unpinned"  : まだ pinning されていない (初回確認 or IndexedDB クリア後)。
 *                   どちらも帯域外で fingerprint を確認させる (サイレント再 pin は不可)。
 *   - "match"     : pinning 済かつ公開鍵が一致 (信頼できる)。
 *   - "mismatch"  : pinning 済だが公開鍵が変わった (すり替えの可能性 → 警告)。
 *
 * @param {{get: Function}} store
 * @param {number} peerId
 * @param {Uint8Array} publicKeyRaw
 * @param {string} [role]
 * @returns {Promise<{status: string, label: string, hashHex: string, pinnedAt: ?string}>}
 */
export async function evaluatePin(store, peerId, publicKeyRaw, role) {
  const { hashHex, label } = await computeFingerprint(publicKeyRaw, role);
  const existing = await store.get(peerId);
  if (!existing) {
    return { status: "unpinned", label, hashHex, pinnedAt: null };
  }
  const status = classifyPin(existing.public_key_sha256, hashHex);
  return { status, label, hashHex, pinnedAt: existing.pinned_at };
}

/**
 * 相手の公開鍵ハッシュを pinning する (帯域外確認後にユーザーが明示操作した時)。
 * @param {{put: Function}} store
 * @param {number} peerId
 * @param {string} hashHex  SHA-256(public_key) の hex
 * @param {string} nowIso  pinning 時刻 (ISO 文字列、呼び出し側で生成)
 * @returns {Promise<void>}
 */
export async function pinKey(store, peerId, hashHex, nowIso) {
  await store.put({
    peer_user_id: Number(peerId),
    public_key_sha256: hashHex,
    pinned_at: nowIso,
  });
}

/**
 * 相手の pinning を解除する。
 * @param {{delete: Function}} store
 * @param {number} peerId
 * @returns {Promise<void>}
 */
export async function unpinKey(store, peerId) {
  await store.delete(peerId);
}
