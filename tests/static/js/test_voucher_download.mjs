// crypto/voucher_download.js (E4 #111) の単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/voucher_download.js",
  import.meta.url,
);
const { voucherImageUrl, fetchAndDecryptVoucherImage, sniffImageMime } =
  await import(M.href);

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


// ============ sniffImageMime ============

test("sniffImageMime: JPEG/PNG/GIF/WebP のマジックナンバー", () => {
  assert.equal(sniffImageMime(new Uint8Array([0xff, 0xd8, 0xff, 0xe0])), "image/jpeg");
  assert.equal(
    sniffImageMime(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])),
    "image/png",
  );
  assert.equal(sniffImageMime(new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61])), "image/gif");
  const webp = new Uint8Array([
    0x52, 0x49, 0x46, 0x46, 0, 0, 0, 0, 0x57, 0x45, 0x42, 0x50,
  ]);
  assert.equal(sniffImageMime(webp), "image/webp");
});

test("sniffImageMime: 判定不能 / 短すぎは octet-stream", () => {
  assert.equal(sniffImageMime(new Uint8Array([1, 2, 3, 4])), "application/octet-stream");
  assert.equal(sniffImageMime(new Uint8Array([1])), "application/octet-stream");
  assert.equal(sniffImageMime(null), "application/octet-stream");
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
    client, userId: 3, voucherId: 7, aadId: 777n, fetchImpl,
  });
  assert.deepEqual(out, plaintext);
  // URL は voucher_id=7、AAD は aad_id=777 で束縛 (別値で区別)。
  assert.deepEqual(client.calls[0].aad, buildAAD("vimg", 3, 777n));
  assert.equal(client.calls[0].ivLen, 12);
});

test("サムネは ?size=thumb + vthumb AAD (aad_id 束縛)", async () => {
  const client = makeMockClient();
  const blob = new Uint8Array(12 + 4 + 16);
  let calledUrl = null;
  const fetchImpl = async (url) => { calledUrl = url; return abResp(blob); };
  await fetchAndDecryptVoucherImage({
    client, userId: 3, voucherId: 7, aadId: 777n, thumb: true, fetchImpl,
  });
  assert.equal(calledUrl, "/ai-journal/voucher/7/image?size=thumb");
  assert.deepEqual(client.calls[0].aad, buildAAD("vthumb", 3, 777n));
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
