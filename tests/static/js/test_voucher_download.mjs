// crypto/voucher_download.js (E4 #111) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/voucher_download.js",
  import.meta.url,
);
const { voucherImageUrl, fetchAndDecryptVoucherImage } = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);


function makeMockClient() {
  return {
    calls: [],
    async decrypt(ct, iv, aad) {
      this.calls.push({ aad: new Uint8Array(aad), ivLen: iv.length });
      // mock: plaintext = ct から末尾 16B (tag) を除いたもの
      return { plaintext: ct.slice(0, ct.length - 16) };
    },
  };
}

function abResp(bytes, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async arrayBuffer() {
      return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    },
  };
}


// ============ voucherImageUrl ============

test("voucherImageUrl: 本体とサムネ", () => {
  assert.equal(voucherImageUrl(42, false), "/ai-journal/voucher/42/image");
  assert.equal(voucherImageUrl(42, true), "/ai-journal/voucher/42/image?size=thumb");
});

test("voucherImageUrl: URL sink をサニタイズ", () => {
  assert.equal(
    voucherImageUrl("1/../x", false),
    "/ai-journal/voucher/1%2F..%2Fx/image",
  );
});


// ============ fetchAndDecryptVoucherImage ============

test("本体を fetch + 復号 (vimg AAD)", async () => {
  const client = makeMockClient();
  // iv(12) || ct(plaintext 'hello' + 16B tag)
  const plaintext = new TextEncoder().encode("hello");
  const blob = new Uint8Array(12 + plaintext.length + 16);
  blob.set(plaintext, 12);
  const fetchImpl = async (url) => {
    assert.equal(url, "/ai-journal/voucher/7/image");
    return abResp(blob);
  };
  const out = await fetchAndDecryptVoucherImage({
    client, userId: 3, voucherId: 7, fetchImpl,
  });
  assert.deepEqual(out, plaintext);
  assert.deepEqual(client.calls[0].aad, buildAAD("vimg", 3, 7));
  assert.equal(client.calls[0].ivLen, 12);
});

test("サムネは ?size=thumb + vthumb AAD", async () => {
  const client = makeMockClient();
  const blob = new Uint8Array(12 + 4 + 16);
  let calledUrl = null;
  const fetchImpl = async (url) => { calledUrl = url; return abResp(blob); };
  await fetchAndDecryptVoucherImage({
    client, userId: 3, voucherId: 7, thumb: true, fetchImpl,
  });
  assert.equal(calledUrl, "/ai-journal/voucher/7/image?size=thumb");
  assert.deepEqual(client.calls[0].aad, buildAAD("vthumb", 3, 7));
});

test("非 2xx は throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => abResp(new Uint8Array(0), 404);
  await assert.rejects(
    () => fetchAndDecryptVoucherImage({ client, userId: 1, voucherId: 1, fetchImpl }),
    /HTTP 404/,
  );
});

test("iv+tag 未満は throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => abResp(new Uint8Array(10));
  await assert.rejects(
    () => fetchAndDecryptVoucherImage({ client, userId: 1, voucherId: 1, fetchImpl }),
    /too short/,
  );
});

test("client 不正は throw", async () => {
  await assert.rejects(
    () => fetchAndDecryptVoucherImage({ client: {}, userId: 1, voucherId: 1 }),
    /client/,
  );
});
