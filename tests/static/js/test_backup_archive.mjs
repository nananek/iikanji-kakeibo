// Phase v5 BU-3: backup_archive.js (encryptBackupArchive / decryptBackupArchive)
// の単体テスト。
//
// Argon2id は重いので、テスト用の固定値モック (32B 鍵を salt から HMAC で派生)
// を DI して暗号化/復号の境界条件をチェックする。実際の Argon2id 連携は
// node-argon2 / hash-wasm を CI に入れる別 PR で integration test として扱う。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/backup_archive.js",
  import.meta.url,
);
const {
  encryptBackupArchive,
  decryptBackupArchive,
  _internals,
} = await import(M.href);


// --- mock Argon2id impl ---
// 32B 鍵を salt から HMAC で派生 (テスト目的、暗号学的強度は問わない)
function makeMockArgon2Impl() {
  return async function mockArgon2id({ password, salt, hashLength }) {
    // password (Uint8Array or string) を Uint8Array に統一
    const passBytes = typeof password === "string"
      ? new TextEncoder().encode(password) : password;
    // HMAC-SHA256(key=salt, data=password) の先頭 hashLength byte
    const cryptoKey = await crypto.subtle.importKey(
      "raw", salt, { name: "HMAC", hash: "SHA-256" },
      false, ["sign"],
    );
    const sig = new Uint8Array(
      await crypto.subtle.sign("HMAC", cryptoKey, passBytes),
    );
    if (sig.byteLength < hashLength) {
      throw new Error(`mock: hashLength ${hashLength} > 32`);
    }
    return sig.slice(0, hashLength);
  };
}


function plaintextBytes(s) {
  return new TextEncoder().encode(s);
}


// --- argument validation ---

test("plaintext が Uint8Array でないと TypeError", async () => {
  await assert.rejects(
    () => encryptBackupArchive("foo", "pw", { argon2Impl: makeMockArgon2Impl() }),
    /Uint8Array/,
  );
});

test("passphrase が空文字だと TypeError", async () => {
  await assert.rejects(
    () => encryptBackupArchive(plaintextBytes("x"), "", {
      argon2Impl: makeMockArgon2Impl(),
    }),
    /passphrase/,
  );
});

test("passphrase が 8 文字未満だと TypeError (encrypt)", async () => {
  await assert.rejects(
    () => encryptBackupArchive(plaintextBytes("x"), "short", {
      argon2Impl: makeMockArgon2Impl(),
    }),
    /at least 8/,
  );
});

test("passphrase が 8 文字未満だと TypeError (decrypt)", async () => {
  const impl = makeMockArgon2Impl();
  const enc = await encryptBackupArchive(
    plaintextBytes("x"), "long-enough-pp", { argon2Impl: impl },
  );
  await assert.rejects(
    () => decryptBackupArchive(enc, "short", { argon2Impl: impl }),
    /at least 8/,
  );
});

test("passphrase が string でないと TypeError", async () => {
  await assert.rejects(
    () => encryptBackupArchive(plaintextBytes("x"), 12345678, {
      argon2Impl: makeMockArgon2Impl(),
    }),
    /must be a string/,
  );
});


// --- header layout ---

test("header の magic / version が固定値", async () => {
  const impl = makeMockArgon2Impl();
  const out = await encryptBackupArchive(
    plaintextBytes("hello"), "longpass", { argon2Impl: impl },
  );
  // magic 8 bytes
  for (let i = 0; i < 8; i++) {
    assert.equal(out[i], _internals.MAGIC[i]);
  }
  // version
  assert.equal(out[8], _internals.VERSION);
  // 全体長 = header + ciphertext (16B GCM tag 込みなので >= header + 5 + 16)
  assert.ok(out.byteLength >= _internals.HEADER_LEN + 5 + 16);
});


// --- round trip ---

test("暗号化→復号で原文が戻る", async () => {
  const impl = makeMockArgon2Impl();
  const original = plaintextBytes("いいかんじ™家計簿 v5 backup");
  const encrypted = await encryptBackupArchive(
    original, "secret-pass-123", { argon2Impl: impl },
  );
  const decrypted = await decryptBackupArchive(
    encrypted, "secret-pass-123", { argon2Impl: impl },
  );
  assert.deepEqual(decrypted, original);
});

test("空 plaintext でも往復可能", async () => {
  const impl = makeMockArgon2Impl();
  const empty = new Uint8Array(0);
  const enc = await encryptBackupArchive(empty, "longpass", { argon2Impl: impl });
  const dec = await decryptBackupArchive(enc, "longpass", { argon2Impl: impl });
  assert.equal(dec.byteLength, 0);
});


// --- security failure modes ---

test("間違ったパスフレーズで decrypt 失敗 (OperationError)", async () => {
  const impl = makeMockArgon2Impl();
  const enc = await encryptBackupArchive(
    plaintextBytes("xx"), "correct-pass", { argon2Impl: impl },
  );
  await assert.rejects(
    () => decryptBackupArchive(enc, "wrong-pass", { argon2Impl: impl }),
    /operation|decrypt|error/i,
  );
});

test("ヘッダ tampering (argon2 params 改ざん) で AAD 不一致", async () => {
  const impl = makeMockArgon2Impl();
  const enc = await encryptBackupArchive(
    plaintextBytes("xx"), "longpass", { argon2Impl: impl },
  );
  // memory_kib (offset 12-15) を 0 に潰す
  enc[12] = enc[13] = enc[14] = enc[15] = 0;
  await assert.rejects(
    () => decryptBackupArchive(enc, "longpass", { argon2Impl: impl }),
    /operation|decrypt|error/i,
  );
});

test("ciphertext tampering で復号失敗", async () => {
  const impl = makeMockArgon2Impl();
  const enc = await encryptBackupArchive(
    plaintextBytes("xx"), "longpass", { argon2Impl: impl },
  );
  // 最終 byte (GCM tag の末尾) を 1 bit 反転
  enc[enc.byteLength - 1] ^= 0x01;
  await assert.rejects(
    () => decryptBackupArchive(enc, "longpass", { argon2Impl: impl }),
    /operation|decrypt|error/i,
  );
});


// --- format errors ---

test("magic が違うと invalid magic", async () => {
  const buf = new Uint8Array(_internals.HEADER_LEN);
  await assert.rejects(
    () => decryptBackupArchive(buf, "longpass", {
      argon2Impl: makeMockArgon2Impl(),
    }),
    /magic/,
  );
});

test("version が違うと unsupported version", async () => {
  const impl = makeMockArgon2Impl();
  const enc = await encryptBackupArchive(
    plaintextBytes("x"), "longpass", { argon2Impl: impl },
  );
  enc[8] = 0xFF;  // version
  await assert.rejects(
    () => decryptBackupArchive(enc, "longpass", { argon2Impl: impl }),
    /version/,
  );
});

test("archive が header 長未満だと too short", async () => {
  await assert.rejects(
    () => decryptBackupArchive(new Uint8Array(10), "longpass", {
      argon2Impl: makeMockArgon2Impl(),
    }),
    /short/,
  );
});

test("archive 長と header の ciphertext_len が不一致だとエラー", async () => {
  const impl = makeMockArgon2Impl();
  const enc = await encryptBackupArchive(
    plaintextBytes("x"), "longpass", { argon2Impl: impl },
  );
  // 末尾 1 byte 削る
  const truncated = enc.slice(0, enc.byteLength - 1);
  await assert.rejects(
    () => decryptBackupArchive(truncated, "longpass", { argon2Impl: impl }),
    /length mismatch/,
  );
});


// --- determinism ---

test("同じ入力でも salt/iv がランダムなので毎回違うバイナリ", async () => {
  const impl = makeMockArgon2Impl();
  const a = await encryptBackupArchive(
    plaintextBytes("same"), "longpass", { argon2Impl: impl },
  );
  const b = await encryptBackupArchive(
    plaintextBytes("same"), "longpass", { argon2Impl: impl },
  );
  assert.notDeepEqual(a, b);
  // ただしどちらも復号できる
  const dec_a = await decryptBackupArchive(a, "longpass", { argon2Impl: impl });
  const dec_b = await decryptBackupArchive(b, "longpass", { argon2Impl: impl });
  assert.deepEqual(dec_a, dec_b);
});
