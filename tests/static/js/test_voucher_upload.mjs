// crypto/voucher_upload.js (E4 #111) の単体テスト。
//
// SharedCryptoClient (実 AES-GCM) は Node で worker が動かないのでモック。
// AAD 束縛・opaque blob (iv||ct) 連結・multipart PUT・file_hash_plain 計算・
// 2 段階 upload オーケストレーションを検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/crypto/voucher_upload.js",
  import.meta.url,
);
const {
  sha256Hex,
  initVoucher,
  encryptVoucher,
  putVoucher,
  uploadEncryptedVoucher,
} = await import(M.href);

const REC = new URL("../../../app/static/js/crypto/record.js", import.meta.url);
const { buildAAD } = await import(REC.href);


// --- mock SharedCryptoClient (AAD を記録、ciphertext = plaintext + 16B) ---

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


// ============ sha256Hex ============

test("sha256Hex: 既知ベクタ (空) ", async () => {
  const h = await sha256Hex(new Uint8Array(0));
  assert.equal(
    h,
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  );
});

test("sha256Hex: ArrayBuffer も受け付ける", async () => {
  const buf = new TextEncoder().encode("abc").buffer;
  const h = await sha256Hex(buf);
  assert.equal(
    h,
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  );
});

test("sha256Hex: 不正な型は TypeError", async () => {
  await assert.rejects(() => sha256Hex("not-bytes"), /Uint8Array or ArrayBuffer/);
});


// ============ initVoucher ============

test("initVoucher: POST /init → {voucherId, aadId} (aad_id は文字列→BigInt)", async () => {
  let captured = null;
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", (url, opts) => {
      captured = opts;
      return jsonResp(201, { ok: true, voucher_id: 42, aad_id: "777" });
    }],
  ]);
  const { voucherId, aadId } = await initVoucher({
    journalEntryId: 7, fetchImpl, csrf: "tok",
  });
  assert.equal(voucherId, 42);
  assert.equal(aadId, 777n);
  assert.equal(captured.method, "POST");
  assert.equal(captured.headers["X-CSRFToken"], "tok");
  assert.deepEqual(JSON.parse(captured.body), { journal_entry_id: 7 });
});

test("initVoucher: 63bit の aad_id を精度欠落なく BigInt 化", async () => {
  // 9223372036854775783 は 2^53 を遥かに超える素数 (Number では丸められる)。
  const big = "9223372036854775783";
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", () =>
      jsonResp(201, { ok: true, voucher_id: 1, aad_id: big })],
  ]);
  const { aadId } = await initVoucher({ fetchImpl, csrf: "x" });
  assert.equal(aadId, BigInt(big));
  assert.equal(aadId.toString(), big);
});

test("initVoucher: journalEntryId 省略時は null を送る", async () => {
  let body = null;
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", (url, opts) => {
      body = JSON.parse(opts.body);
      return jsonResp(201, { ok: true, voucher_id: 1, aad_id: "5" });
    }],
  ]);
  await initVoucher({ fetchImpl, csrf: "x" });
  assert.equal(body.journal_entry_id, null);
});

test("initVoucher: 非 2xx は throw", async () => {
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", () => jsonResp(403, { error: "代理閲覧" })],
  ]);
  await assert.rejects(
    () => initVoucher({ fetchImpl, csrf: "x" }),
    /HTTP 403/,
  );
});


// ============ encryptVoucher ============

test("encryptVoucher: 画像/サムネ/メタを正しい AAD (aad_id 束縛) で暗号化", async () => {
  const client = makeMockClient();
  const userId = 5;
  const aadId = 99n;
  const imageBytes = new Uint8Array([1, 2, 3, 4]);
  const thumbBytes = new Uint8Array([9, 9]);

  const out = await encryptVoucher({
    client, userId, aadId, imageBytes, thumbBytes,
    meta: { original_filename: "r.jpg", image_mime: "image/jpeg" },
  });

  // imageCt = iv(12) || ct(image+16)
  assert.equal(out.imageCt.byteLength, 12 + 4 + 16);
  assert.equal(out.thumbCt.byteLength, 12 + 2 + 16);
  assert.equal(out.metaIv.byteLength, 12);
  assert.ok(out.metaBlob.byteLength > 0);
  // file_hash_plain = SHA-256(平文画像)
  assert.equal(out.fileHashPlain, await sha256Hex(imageBytes));

  // 3 回 encrypt が呼ばれ、それぞれ vimg/vthumb/vmeta AAD (voucher_id ではなく
  // aad_id 束縛)
  assert.equal(client.calls.length, 3);
  assert.deepEqual(client.calls[0].aad, buildAAD("vimg", userId, aadId));
  assert.deepEqual(client.calls[1].aad, buildAAD("vthumb", userId, aadId));
  assert.deepEqual(client.calls[2].aad, buildAAD("vmeta", userId, aadId));
});

test("encryptVoucher: サムネ省略時 thumbCt=null・encrypt 2 回", async () => {
  const client = makeMockClient();
  const out = await encryptVoucher({
    client, userId: 1, aadId: 2n,
    imageBytes: new Uint8Array([7]),
  });
  assert.equal(out.thumbCt, null);
  assert.equal(client.calls.length, 2);  // vimg + vmeta のみ
});

test("encryptVoucher: meta は {v:1, ...} を含む", async () => {
  const captured = [];
  const client = {
    async encrypt(pt, aad) {
      captured.push(pt);
      return { ciphertext: new Uint8Array(pt.length + 16), iv: new Uint8Array(12) };
    },
  };
  await encryptVoucher({
    client, userId: 1, aadId: 2n,
    imageBytes: new Uint8Array([1]),
    meta: { original_filename: "a.png" },
  });
  // 2 番目の encrypt = meta JSON
  const metaJson = JSON.parse(new TextDecoder().decode(captured[1]));
  assert.equal(metaJson.v, 1);
  assert.equal(metaJson.original_filename, "a.png");
});

test("encryptVoucher: client 不正で throw", async () => {
  await assert.rejects(
    () => encryptVoucher({ client: {}, userId: 1, aadId: 1n, imageBytes: new Uint8Array([1]) }),
    /client/,
  );
});

test("encryptVoucher: 空画像で throw", async () => {
  await assert.rejects(
    () => encryptVoucher({ client: makeMockClient(), userId: 1, aadId: 1n, imageBytes: new Uint8Array(0) }),
    /empty/,
  );
});

test("encryptVoucher: 10MB 超で throw", async () => {
  await assert.rejects(
    () => encryptVoucher({
      client: makeMockClient(), userId: 1, aadId: 1n,
      imageBytes: new Uint8Array(10 * 1024 * 1024 + 1),
    }),
    /10MB/,
  );
});


// ============ putVoucher ============

test("putVoucher: multipart で全フィールドを送る", async () => {
  let captured = null;
  const fetchImpl = mockFetch([
    [/\/api\/v1\/vouchers\/77$/, (url, opts) => {
      captured = opts;
      return jsonResp(200, { ok: true, voucher_id: 77, file_hash_cipher: "ab" });
    }],
  ]);
  const res = await putVoucher({
    voucherId: 77,
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
  // FormData が body
  assert.ok(captured.body instanceof FormData);
  assert.ok(captured.body.get("image_ct"));
  assert.ok(captured.body.get("thumb_ct"));
  assert.equal(captured.body.get("file_hash_plain"), "a".repeat(64));
  assert.equal(typeof captured.body.get("meta_blob"), "string");
  // multipart boundary を fetch に任せるため Content-Type は未指定
  assert.equal(captured.headers["Content-Type"], undefined);
});

test("putVoucher: thumbCt なしなら thumb_ct を含めない", async () => {
  let body = null;
  const fetchImpl = mockFetch([
    [/\/vouchers\/5$/, (url, opts) => {
      body = opts.body;
      return jsonResp(200, { ok: true });
    }],
  ]);
  await putVoucher({
    voucherId: 5, imageCt: new Uint8Array([1]), thumbCt: null,
    metaBlob: new Uint8Array([1]), metaIv: new Uint8Array(12),
    fileHashPlain: "b".repeat(64), fetchImpl, csrf: "x",
  });
  assert.equal(body.get("thumb_ct"), null);
});

test("putVoucher: 非 2xx は throw", async () => {
  const fetchImpl = mockFetch([
    [/\/vouchers\/9$/, () => jsonResp(409, { error: "上書き禁止" })],
  ]);
  await assert.rejects(
    () => putVoucher({
      voucherId: 9, imageCt: new Uint8Array([1]), thumbCt: null,
      metaBlob: new Uint8Array([1]), metaIv: new Uint8Array(12),
      fileHashPlain: "c".repeat(64), fetchImpl, csrf: "x",
    }),
    /HTTP 409/,
  );
});


// ============ uploadEncryptedVoucher (オーケストレーション) ============

function fakeFile(bytes, name, type) {
  const blob = new Blob([bytes], { type });
  // File 風: name プロパティを付与
  blob.name = name;
  return blob;
}

test("uploadEncryptedVoucher: init→encrypt→PUT 一連", async () => {
  const client = makeMockClient();
  const puts = [];
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", () =>
      jsonResp(201, { ok: true, voucher_id: 123, aad_id: "777" })],
    [/\/api\/v1\/vouchers\/123$/, (url, opts) => {
      puts.push(opts);
      return jsonResp(200, { ok: true, voucher_id: 123, file_hash_cipher: "deadbeef" });
    }],
  ]);

  const file = fakeFile(new Uint8Array([10, 20, 30]), "receipt.jpg", "image/jpeg");
  const thumb = new Uint8Array([1, 1]);

  const res = await uploadEncryptedVoucher({
    client, userId: 8, file, journalEntryId: 55,
    makeThumbnail: async () => thumb,
    fetchImpl, csrf: "tok",
  });

  assert.equal(res.voucherId, 123);
  assert.equal(res.file_hash_cipher, "deadbeef");
  assert.equal(puts.length, 1);  // PUT は /vouchers/123 (route regex で検証済)
  // image_ct の AAD は aad_id=777 で束縛される (voucher_id=123 ではないことを
  // 別値で確認)。
  assert.deepEqual(client.calls[0].aad, buildAAD("vimg", 8, 777n));
});

test("uploadEncryptedVoucher: makeThumbnail 省略でサムネなし", async () => {
  const client = makeMockClient();
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", () =>
      jsonResp(201, { ok: true, voucher_id: 1, aad_id: "5" })],
    [/\/vouchers\/1$/, () => jsonResp(200, { ok: true })],
  ]);
  const file = fakeFile(new Uint8Array([1]), "x.png", "image/png");
  await uploadEncryptedVoucher({ client, userId: 1, file, fetchImpl, csrf: "x" });
  // vimg + vmeta の 2 回のみ (vthumb なし)
  assert.equal(client.calls.length, 2);
});


// ============ _csrf (document fallback) ============

test("_csrf: document があれば meta tag から取得", async () => {
  // document スタブを差し込んで meta 分岐をカバー
  globalThis.document = {
    querySelector: (sel) =>
      sel === 'meta[name="csrf-token"]'
        ? { getAttribute: () => "csrf-from-meta" }
        : null,
  };
  let header = null;
  const fetchImpl = mockFetch([
    ["/api/v1/vouchers/init", (url, opts) => {
      header = opts.headers["X-CSRFToken"];
      return jsonResp(201, { ok: true, voucher_id: 1, aad_id: "5" });
    }],
  ]);
  try {
    await initVoucher({ fetchImpl });  // csrf 省略 → _csrf() 経由
    assert.equal(header, "csrf-from-meta");
  } finally {
    delete globalThis.document;
  }
});
