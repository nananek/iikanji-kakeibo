// crypto/keypair.js (E5 #112 PR-A) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/keypair.js",
  import.meta.url,
);
const { generateX25519KeyPair, getKeyPair, putKeyPair, ensureKeyPair } =
  await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { uint64BE } = await import(REC.href);
const B64 = new URL("../../../app/static/js/crypto/b64.js", import.meta.url);
const { b64encode, b64decode } = await import(B64.href);

const TEXT_ENC = new TextEncoder();


function expectedPrivAAD(userId) {
  const prefix = TEXT_ENC.encode("x25519-priv");
  const nul = TEXT_ENC.encode("\0");
  const uid = uint64BE(userId);
  const out = new Uint8Array(prefix.length + nul.length + uid.length);
  out.set(prefix, 0);
  out.set(nul, prefix.length);
  out.set(uid, prefix.length + nul.length);
  return out;
}

function makeMockClient() {
  return {
    calls: [],
    async encrypt(plaintext, aad) {
      this.calls.push({
        plaintext: new Uint8Array(plaintext),
        aad: new Uint8Array(aad),
      });
      return {
        ciphertext: new Uint8Array([1, 2, 3, 4]),
        iv: new Uint8Array(12).fill(7),
      };
    },
  };
}

/** GET/PUT を記録するモック fetch。getResponse で GET の戻り値を制御。 */
function makeMockFetch(getJson) {
  const calls = [];
  const fn = async (url, opts = {}) => {
    calls.push({ url, opts });
    const method = opts.method || "GET";
    if (method === "GET") {
      return { ok: true, status: 200, async json() { return getJson; } };
    }
    // PUT
    return { ok: true, status: 200, async json() { return { ok: true }; } };
  };
  fn.calls = calls;
  return fn;
}


// ============ generateX25519KeyPair ============

test("generateX25519KeyPair returns 32B public + 48B pkcs8 private", async () => {
  const { publicRaw, privatePkcs8 } = await generateX25519KeyPair();
  assert.ok(publicRaw instanceof Uint8Array);
  assert.equal(publicRaw.length, 32);
  assert.ok(privatePkcs8 instanceof Uint8Array);
  assert.equal(privatePkcs8.length, 48);
});


// ============ getKeyPair ============

test("getKeyPair decodes base64 fields", async () => {
  const fetchImpl = makeMockFetch({
    public_key: b64encode(new Uint8Array(32).fill(9)),
    encrypted_private_key: b64encode(new Uint8Array([5, 6])),
    private_key_iv: b64encode(new Uint8Array(12).fill(3)),
  });
  const res = await getKeyPair(fetchImpl);
  assert.deepEqual(res.public_key, new Uint8Array(32).fill(9));
  assert.deepEqual(res.encrypted_private_key, new Uint8Array([5, 6]));
  assert.deepEqual(res.private_key_iv, new Uint8Array(12).fill(3));
});

test("getKeyPair maps nulls to null", async () => {
  const fetchImpl = makeMockFetch({
    public_key: null,
    encrypted_private_key: null,
    private_key_iv: null,
  });
  const res = await getKeyPair(fetchImpl);
  assert.equal(res.public_key, null);
  assert.equal(res.encrypted_private_key, null);
  assert.equal(res.private_key_iv, null);
});


// ============ putKeyPair ============

test("putKeyPair sends base64-encoded body to PUT", async () => {
  const fetchImpl = makeMockFetch({});
  await putKeyPair(
    {
      publicRaw: new Uint8Array(32).fill(1),
      encBlob: new Uint8Array([8, 9]),
      iv: new Uint8Array(12).fill(2),
    },
    fetchImpl,
  );
  const put = fetchImpl.calls.find((c) => (c.opts.method || "GET") === "PUT");
  assert.ok(put, "PUT was issued");
  const body = JSON.parse(put.opts.body);
  assert.deepEqual(b64decode(body.public_key), new Uint8Array(32).fill(1));
  assert.deepEqual(b64decode(body.encrypted_private_key), new Uint8Array([8, 9]));
  assert.deepEqual(b64decode(body.private_key_iv), new Uint8Array(12).fill(2));
});


// ============ ensureKeyPair ============

test("ensureKeyPair returns false and skips PUT when keypair already set", async () => {
  const fetchImpl = makeMockFetch({
    public_key: b64encode(new Uint8Array(32).fill(4)),
    encrypted_private_key: b64encode(new Uint8Array([1])),
    private_key_iv: b64encode(new Uint8Array(12)),
  });
  const client = makeMockClient();
  const created = await ensureKeyPair(client, 42, fetchImpl);
  assert.equal(created, false);
  assert.equal(client.calls.length, 0, "MK encrypt not called");
  assert.ok(
    !fetchImpl.calls.some((c) => (c.opts.method || "GET") === "PUT"),
    "no PUT issued",
  );
});

test("ensureKeyPair generates, wraps with correct AAD, and PUTs when unset", async () => {
  const fetchImpl = makeMockFetch({
    public_key: null,
    encrypted_private_key: null,
    private_key_iv: null,
  });
  const client = makeMockClient();
  const created = await ensureKeyPair(client, 7, fetchImpl);
  assert.equal(created, true);

  // MK encrypt が pkcs8 秘密鍵 + 正しい AAD で呼ばれた
  assert.equal(client.calls.length, 1);
  assert.equal(client.calls[0].plaintext.length, 48);
  assert.deepEqual(client.calls[0].aad, expectedPrivAAD(7));

  // PUT body は生成した公開鍵 + client.encrypt の暗号文/iv
  const put = fetchImpl.calls.find((c) => (c.opts.method || "GET") === "PUT");
  const body = JSON.parse(put.opts.body);
  assert.equal(b64decode(body.public_key).length, 32);
  assert.deepEqual(b64decode(body.encrypted_private_key), new Uint8Array([1, 2, 3, 4]));
  assert.deepEqual(b64decode(body.private_key_iv), new Uint8Array(12).fill(7));
});

test("ensureKeyPair throws when userId missing", async () => {
  const client = makeMockClient();
  await assert.rejects(
    () => ensureKeyPair(client, null, makeMockFetch({ public_key: null })),
    /userId is required/,
  );
});

test("ensureKeyPair throws when client invalid", async () => {
  await assert.rejects(
    () => ensureKeyPair({}, 1, makeMockFetch({ public_key: null })),
    /SharedCryptoClient/,
  );
});
