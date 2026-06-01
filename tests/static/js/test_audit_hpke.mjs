// E5 #112 PR-D: HPKE モジュール (hpke_suite.js / audit_hpke.js / worker hpkeOpen) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const SUITE = new URL("../../../app/static/js/crypto/hpke_suite.js", import.meta.url);
const { hpkeSeal, hpkeOpenWithRawPriv, pkcs8ToRawScalar } = await import(SUITE.href);

const AH = new URL("../../../app/static/js/crypto/audit_hpke.js", import.meta.url);
const {
  uint32BE, packageAAD, responseAAD, snapshotHash,
  sealAuditPackage, sealAuditResponse,
} = await import(AH.href);

const CORE = new URL("../../../app/static/js/crypto/shared-worker-core.js", import.meta.url);
const { MasterKeyState } = await import(CORE.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { uint64BE } = await import(REC.href);


async function genKeyPair() {
  const kp = await crypto.subtle.generateKey({ name: "X25519" }, true, ["deriveBits"]);
  const pubRaw = new Uint8Array(await crypto.subtle.exportKey("raw", kp.publicKey));
  const privPkcs8 = new Uint8Array(await crypto.subtle.exportKey("pkcs8", kp.privateKey));
  return { pubRaw, privPkcs8 };
}

const u8 = (s) => new TextEncoder().encode(s);


// ===== hpke_suite =====

test("hpkeSeal -> hpkeOpenWithRawPriv round-trip", async () => {
  const { pubRaw, privPkcs8 } = await genKeyPair();
  const aad = u8("ap-aad");
  const { enc, ciphertext } = await hpkeSeal(pubRaw, u8("secret snapshot"), aad);
  assert.equal(enc.length, 32, "enc is 32B");
  const raw = pkcs8ToRawScalar(privPkcs8);
  const pt = await hpkeOpenWithRawPriv(raw, enc, ciphertext, aad);
  assert.equal(new TextDecoder().decode(pt), "secret snapshot");
});

test("hpkeOpen fails on AAD mismatch", async () => {
  const { pubRaw, privPkcs8 } = await genKeyPair();
  const { enc, ciphertext } = await hpkeSeal(pubRaw, u8("x"), u8("right-aad"));
  const raw = pkcs8ToRawScalar(privPkcs8);
  await assert.rejects(() => hpkeOpenWithRawPriv(raw, enc, ciphertext, u8("wrong-aad")));
});

test("hpkeOpen fails with a different recipient key", async () => {
  const a = await genKeyPair();
  const b = await genKeyPair();
  const aad = u8("aad");
  const { enc, ciphertext } = await hpkeSeal(a.pubRaw, u8("x"), aad);
  await assert.rejects(
    () => hpkeOpenWithRawPriv(pkcs8ToRawScalar(b.privPkcs8), enc, ciphertext, aad),
  );
});

test("pkcs8ToRawScalar returns last 32B and validates length", async () => {
  const { privPkcs8 } = await genKeyPair();
  assert.equal(privPkcs8.length, 48);
  const raw = pkcs8ToRawScalar(privPkcs8);
  assert.equal(raw.length, 32);
  assert.deepEqual(raw, privPkcs8.slice(16));
  assert.throws(() => pkcs8ToRawScalar(new Uint8Array(32)), /48 bytes/);
});


// ===== audit_hpke: AAD / hash =====

test("uint32BE encodes big-endian", () => {
  assert.deepEqual(uint32BE(1), new Uint8Array([0, 0, 0, 1]));
  assert.deepEqual(uint32BE(0x01020304), new Uint8Array([1, 2, 3, 4]));
  assert.throws(() => uint32BE(-1));
  assert.throws(() => uint32BE(0x1_0000_0000));
});

test("packageAAD = 'ap' + uint64BE(grant) + uint32BE(round) (14B)", () => {
  const aad = packageAAD(7, 3);
  assert.equal(aad.length, 14);
  assert.deepEqual(aad.slice(0, 2), u8("ap"));
  assert.deepEqual(aad.slice(2, 10), uint64BE(7));
  assert.deepEqual(aad.slice(10, 14), uint32BE(3));
});

test("responseAAD = 'ar' + uint64BE(packageId) (10B)", () => {
  const aad = responseAAD(42);
  assert.equal(aad.length, 10);
  assert.deepEqual(aad.slice(0, 2), u8("ar"));
  assert.deepEqual(aad.slice(2, 10), uint64BE(42));
});

test("snapshotHash is SHA-256 (32B) and stable", async () => {
  const h1 = await snapshotHash(u8("data"));
  const h2 = await snapshotHash(u8("data"));
  assert.equal(h1.length, 32);
  assert.deepEqual(h1, h2);
  assert.notDeepEqual(h1, await snapshotHash(u8("other")));
});

test("sealAuditPackage produces enc/ciphertext/hash that open with matching AAD", async () => {
  const { pubRaw, privPkcs8 } = await genKeyPair();
  const plaintext = u8(JSON.stringify({ v: 1, level: 1, trial_balance: {} }));
  const { ephemeralPubkey, ciphertext, snapshotHash: hash } =
    await sealAuditPackage(pubRaw, plaintext, 11, 2);
  assert.equal(ephemeralPubkey.length, 32);
  assert.deepEqual(hash, await snapshotHash(plaintext));
  // 受信側は packageAAD(grant, round) で open できる
  const pt = await hpkeOpenWithRawPriv(
    pkcs8ToRawScalar(privPkcs8), ephemeralPubkey, ciphertext, packageAAD(11, 2),
  );
  assert.deepEqual(pt, plaintext);
  // round 違いの AAD では open 失敗
  await assert.rejects(() => hpkeOpenWithRawPriv(
    pkcs8ToRawScalar(privPkcs8), ephemeralPubkey, ciphertext, packageAAD(11, 3),
  ));
});

test("sealAuditResponse opens with responseAAD(packageId)", async () => {
  const { pubRaw, privPkcs8 } = await genKeyPair();
  const plaintext = u8(JSON.stringify({ type: "revision" }));
  const { ephemeralPubkey, ciphertext } = await sealAuditResponse(pubRaw, plaintext, 99);
  const pt = await hpkeOpenWithRawPriv(
    pkcs8ToRawScalar(privPkcs8), ephemeralPubkey, ciphertext, responseAAD(99),
  );
  assert.deepEqual(pt, plaintext);
});


// ===== worker core hpkeOpen (MK で秘密鍵をアンラップ → HPKE open) =====

test("MasterKeyState.hpkeOpen decrypts a sealed package end-to-end", async () => {
  const { pubRaw, privPkcs8 } = await genKeyPair();
  const state = new MasterKeyState();
  // MK を設定 (worker 内 32B 乱数)
  await state.handle({ type: "setKey", id: 1, rawKey: crypto.getRandomValues(new Uint8Array(32)) });

  // owner が seal (相手 = この鍵ペアの所有者)
  const plaintext = u8("worker-open snapshot");
  const sealed = await sealAuditPackage(pubRaw, plaintext, 5, 1);

  // 秘密鍵 (pkcs8) を MK で暗号化して保管している想定を再現: privAad で encrypt
  const privAad = u8("x25519-priv-aad");
  const encRes = await state.handle({
    type: "encrypt", id: 2, plaintext: privPkcs8.slice(), aad: privAad,
  });

  // worker 内で hpkeOpen: MK で秘密鍵をアンラップ → HPKE open
  const out = await state.handle({
    type: "hpkeOpen", id: 3,
    encryptedPrivateKey: encRes.result.ciphertext,
    privIv: encRes.result.iv,
    privAad,
    enc: sealed.ephemeralPubkey,
    ciphertext: sealed.ciphertext,
    aad: packageAAD(5, 1),
  });
  assert.equal(out.broadcast, null);
  assert.deepEqual(out.result.plaintext, plaintext);
});

test("MasterKeyState.hpkeOpen throws without MK", async () => {
  const state = new MasterKeyState();
  await assert.rejects(
    () => state.handle({
      type: "hpkeOpen", id: 1,
      encryptedPrivateKey: new Uint8Array(64), privIv: new Uint8Array(12),
      privAad: new Uint8Array(0), enc: new Uint8Array(32),
      ciphertext: new Uint8Array(16), aad: new Uint8Array(0),
    }),
    /master key not set/,
  );
});
