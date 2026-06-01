// crypto/ai_upload.js (E5 #111) の単体テスト。
//
// AI 下書きの 2 段階 E2EE upload (init → encrypt → PUT)。暗号化は
// voucher_upload.encryptVoucher を再利用するため、ここでは init/put/
// オーケストレーションと CSRF fallback を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/ai_upload.js",
  import.meta.url,
);
const { initAiDraft, putAiDraft, uploadEncryptedDraft } = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);


function makeMockClient() {
  return {
    calls: [],
    async encrypt(plaintext, aad) {
      const iv = new Uint8Array(12);
      crypto.getRandomValues(iv);
      const ciphertext = new Uint8Array(plaintext.length + 16);
      ciphertext.set(plaintext, 0);
      this.calls.push({ aad: new Uint8Array(aad), plaintextLen: plaintext.length });
      return { ciphertext, iv };
    },
  };
}

function mockFetch(routes) {
  return async function (url, opts) {
    for (const [pattern, handler] of routes) {
      if (typeof pattern === "string") {
        if (url === pattern) return handler(url, opts);
      } else if (pattern.test(url)) {
        return handler(url, opts);
      }
    }
    return { ok: false, status: 404, json: async () => ({ error: "not found" }) };
  };
}

function jsonResp(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function fakeFile(bytes, name, type) {
  const blob = new Blob([bytes], { type });
  blob.name = name;
  return blob;
}


// ============ initAiDraft ============

test("initAiDraft: POST /init → {draftId, aadId} (aad_id は文字列→BigInt)", async () => {
  let captured = null;
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", (url, opts) => {
      captured = opts;
      return jsonResp(201, { ok: true, draft_id: 42, aad_id: "777" });
    }],
  ]);
  const { draftId, aadId } = await initAiDraft({
    comment: "メモ", fetchImpl, csrf: "tok",
  });
  assert.equal(draftId, 42);
  assert.equal(aadId, 777n);
  assert.equal(captured.method, "POST");
  assert.equal(captured.headers["X-CSRFToken"], "tok");
  assert.deepEqual(JSON.parse(captured.body), { comment: "メモ" });
});

test("initAiDraft: 63bit の aad_id を精度欠落なく BigInt 化", async () => {
  const big = "9223372036854775783";
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", () =>
      jsonResp(201, { ok: true, draft_id: 1, aad_id: big })],
  ]);
  const { aadId } = await initAiDraft({ fetchImpl, csrf: "x" });
  assert.equal(aadId.toString(), big);
});

test("initAiDraft: comment 省略時は null を送る", async () => {
  let body = null;
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", (url, opts) => {
      body = JSON.parse(opts.body);
      return jsonResp(201, { ok: true, draft_id: 1, aad_id: "5" });
    }],
  ]);
  await initAiDraft({ fetchImpl, csrf: "x" });
  assert.equal(body.comment, null);
});

test("initAiDraft: aad_id が無い応答は明示エラー (BigInt(null) を防ぐ)", async () => {
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", () => jsonResp(201, { ok: true, draft_id: 1 })],
  ]);
  await assert.rejects(() => initAiDraft({ fetchImpl, csrf: "x" }), /aad_id/);
});

test("initAiDraft: 非 2xx は throw", async () => {
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", () => jsonResp(403, { error: "代理閲覧" })],
  ]);
  await assert.rejects(() => initAiDraft({ fetchImpl, csrf: "x" }), /HTTP 403/);
});


// ============ putAiDraft ============

test("putAiDraft: multipart で全フィールドを送る (PUT /ai/uploads/<id>)", async () => {
  let captured = null;
  const fetchImpl = mockFetch([
    [/\/api\/v1\/ai\/uploads\/77$/, (url, opts) => {
      captured = opts;
      return jsonResp(200, { ok: true, draft_id: 77, status: "pending",
                             file_hash_cipher: "ab" });
    }],
  ]);
  const res = await putAiDraft({
    draftId: 77,
    imageCt: new Uint8Array([1, 2, 3]),
    thumbCt: new Uint8Array([4, 5]),
    metaBlob: new Uint8Array([6]),
    metaIv: new Uint8Array(12),
    fileHashPlain: "a".repeat(64),
    fetchImpl, csrf: "tok",
  });
  assert.equal(res.file_hash_cipher, "ab");
  assert.equal(captured.method, "PUT");
  assert.equal(captured.headers["X-CSRFToken"], "tok");
  assert.ok(captured.body instanceof FormData);
  assert.ok(captured.body.get("image_ct"));
  assert.ok(captured.body.get("thumb_ct"));
  assert.equal(captured.body.get("file_hash_plain"), "a".repeat(64));
  assert.equal(typeof captured.body.get("meta_blob"), "string");
  assert.equal(captured.headers["Content-Type"], undefined);
});

test("putAiDraft: thumbCt なしなら thumb_ct を含めない", async () => {
  let body = null;
  const fetchImpl = mockFetch([
    [/\/ai\/uploads\/5$/, (url, opts) => {
      body = opts.body;
      return jsonResp(200, { ok: true });
    }],
  ]);
  await putAiDraft({
    draftId: 5, imageCt: new Uint8Array([1]), thumbCt: null,
    metaBlob: new Uint8Array([1]), metaIv: new Uint8Array(12),
    fileHashPlain: "b".repeat(64), fetchImpl, csrf: "x",
  });
  assert.equal(body.get("thumb_ct"), null);
});

test("putAiDraft: 非 2xx は throw", async () => {
  const fetchImpl = mockFetch([
    [/\/ai\/uploads\/9$/, () => jsonResp(409, { error: "上書き禁止" })],
  ]);
  await assert.rejects(
    () => putAiDraft({
      draftId: 9, imageCt: new Uint8Array([1]), thumbCt: null,
      metaBlob: new Uint8Array([1]), metaIv: new Uint8Array(12),
      fileHashPlain: "c".repeat(64), fetchImpl, csrf: "x",
    }),
    /HTTP 409/,
  );
});


// ============ uploadEncryptedDraft (オーケストレーション) ============

test("uploadEncryptedDraft: init→encrypt→PUT 一連、aad_id 束縛", async () => {
  const client = makeMockClient();
  const puts = [];
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", () =>
      jsonResp(201, { ok: true, draft_id: 123, aad_id: "777" })],
    [/\/api\/v1\/ai\/uploads\/123$/, (url, opts) => {
      puts.push(opts);
      return jsonResp(200, { ok: true, draft_id: 123, status: "pending",
                             file_hash_cipher: "deadbeef" });
    }],
  ]);
  const file = fakeFile(new Uint8Array([10, 20, 30]), "receipt.jpg", "image/jpeg");

  const res = await uploadEncryptedDraft({
    client, userId: 8, file, comment: "x",
    makeThumbnail: async () => new Uint8Array([1, 1]),
    fetchImpl, csrf: "tok",
  });

  assert.equal(res.draftId, 123);
  assert.equal(res.aadId, 777n);
  assert.equal(res.file_hash_cipher, "deadbeef");
  assert.equal(puts.length, 1);
  // image_ct の AAD は aad_id=777 束縛 (証憑と同ドメイン vimg)。
  assert.deepEqual(client.calls[0].aad, buildAAD("vimg", 8, 777n));
});

test("uploadEncryptedDraft: makeThumbnail 省略でサムネなし (encrypt 2 回)", async () => {
  const client = makeMockClient();
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", () =>
      jsonResp(201, { ok: true, draft_id: 1, aad_id: "5" })],
    [/\/ai\/uploads\/1$/, () => jsonResp(200, { ok: true })],
  ]);
  const file = fakeFile(new Uint8Array([1]), "x.png", "image/png");
  await uploadEncryptedDraft({ client, userId: 1, file, fetchImpl, csrf: "x" });
  assert.equal(client.calls.length, 2);  // vimg + vmeta のみ
});


// ============ _csrf (document fallback) ============

test("_csrf: document があれば meta tag から取得", async () => {
  globalThis.document = {
    querySelector: (sel) =>
      sel === 'meta[name="csrf-token"]'
        ? { getAttribute: () => "csrf-from-meta" }
        : null,
  };
  let header = null;
  const fetchImpl = mockFetch([
    ["/api/v1/ai/uploads/init", (url, opts) => {
      header = opts.headers["X-CSRFToken"];
      return jsonResp(201, { ok: true, draft_id: 1, aad_id: "5" });
    }],
  ]);
  try {
    await initAiDraft({ fetchImpl });  // csrf 省略 → _csrf() 経由
    assert.equal(header, "csrf-from-meta");
  } finally {
    delete globalThis.document;
  }
});
