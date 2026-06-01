// crypto/ai_draft_download.js (E5 #111) の単体テスト。
//
// 暗号化 AI 下書き画像の fetch + 復号。voucher_download と同形 (URL のみ下書き
// エンドポイント、AAD ドメイン vimg/vthumb は証憑と共通)。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/ai_draft_download.js",
  import.meta.url,
);
const { draftImageUrl, fetchAndDecryptDraftImage, sniffImageMime } =
  await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);


function makeMockClient() {
  return {
    calls: [],
    async decrypt(ct, iv, aad) {
      this.calls.push({ aad: new Uint8Array(aad), ivLen: iv.length });
      return { plaintext: ct.slice(0, ct.length - 16) };
    },
  };
}

function abResp(bytes, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async arrayBuffer() {
      return bytes.buffer.slice(
        bytes.byteOffset, bytes.byteOffset + bytes.byteLength,
      );
    },
  };
}


// ============ draftImageUrl ============

test("draftImageUrl: 本体とサムネ", () => {
  assert.equal(draftImageUrl(42, false), "/ai-journal/drafts/42/image");
  assert.equal(draftImageUrl(42, true), "/ai-journal/drafts/42/image?size=thumb");
});

test("draftImageUrl: URL sink をサニタイズ", () => {
  assert.equal(
    draftImageUrl("1/../x", false),
    "/ai-journal/drafts/1%2F..%2Fx/image",
  );
});


// ============ sniffImageMime 再エクスポート ============

test("sniffImageMime: voucher_download から再エクスポートされている", () => {
  assert.equal(sniffImageMime(new Uint8Array([0xff, 0xd8, 0xff, 0xe0])), "image/jpeg");
  assert.equal(sniffImageMime(new Uint8Array([1, 2, 3, 4])), "application/octet-stream");
});


// ============ fetchAndDecryptDraftImage ============

test("本体を fetch + 復号 (vimg AAD、aad_id 束縛)", async () => {
  const client = makeMockClient();
  const plaintext = new TextEncoder().encode("hello");
  const blob = new Uint8Array(12 + plaintext.length + 16);
  blob.set(plaintext, 12);
  const fetchImpl = async (url) => {
    assert.equal(url, "/ai-journal/drafts/7/image");
    return abResp(blob);
  };
  const out = await fetchAndDecryptDraftImage({
    client, userId: 3, draftId: 7, aadId: 777n, fetchImpl,
  });
  assert.deepEqual(out, plaintext);
  assert.deepEqual(client.calls[0].aad, buildAAD("vimg", 3, 777n));
  assert.equal(client.calls[0].ivLen, 12);
});

test("サムネは ?size=thumb + vthumb AAD", async () => {
  const client = makeMockClient();
  const blob = new Uint8Array(12 + 4 + 16);
  let calledUrl = null;
  const fetchImpl = async (url) => { calledUrl = url; return abResp(blob); };
  await fetchAndDecryptDraftImage({
    client, userId: 3, draftId: 7, aadId: 777n, thumb: true, fetchImpl,
  });
  assert.equal(calledUrl, "/ai-journal/drafts/7/image?size=thumb");
  assert.deepEqual(client.calls[0].aad, buildAAD("vthumb", 3, 777n));
});

test("非 2xx は throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => abResp(new Uint8Array(0), 404);
  await assert.rejects(
    () => fetchAndDecryptDraftImage({ client, userId: 1, draftId: 1, fetchImpl }),
    /HTTP 404/,
  );
});

test("iv+tag 未満は throw", async () => {
  const client = makeMockClient();
  const fetchImpl = async () => abResp(new Uint8Array(10));
  await assert.rejects(
    () => fetchAndDecryptDraftImage({ client, userId: 1, draftId: 1, fetchImpl }),
    /too short/,
  );
});

test("client 不正は throw", async () => {
  await assert.rejects(
    () => fetchAndDecryptDraftImage({ client: {}, userId: 1, draftId: 1 }),
    /client/,
  );
});
