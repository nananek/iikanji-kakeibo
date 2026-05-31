// E4 (#111) 証憑画像 E2EE の統合 E2E。
//
// 実 Firefox + 実サーバ + 実 Postgres に対して、証憑画像のクライアント完結
// 暗号化往復を検証する:
//   canvas サムネ生成 → AES-GCM 暗号化 → 2 段階 upload (init→PUT) → サーバが
//   暗号文を保存 → fetch → 復号 → 原画像と一致。
// さらに証憑一覧 (/vouchers) で暗号化証憑が復号 <img> として表示されることを確認。
//
// MK は SharedCryptoClient.setKey() で生 32B を直接注入する。Argon2id パス
// フレーズ派生 (hash-wasm CDN 依存で CI のネットワーク制約下では不可) を回避し、
// CI でも実行可能にするため。暗号化/復号 (AES-GCM in worker) と upload/表示の
// グルーを実地検証することが目的で、鍵派生自体は別途 JS 単体テストで網羅済み。

import { test, expect } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_test";
const PASSWORD = "e2e_pass_12345";

// 16x16 RGBA PNG (createImageBitmap でデコード可能なテスト画像)。
const PNG_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAHUlEQVR4nGO8o6Hxn4ECwESJ5lEDRg0YNWAwGQAADqcCS0agyUcAAAAASUVORK5CYII=";

async function login(page, retries = 2) {
  for (let i = 0; i <= retries; i++) {
    try {
      await page.goto(`${BASE_URL}/login`);
      await page.fill('input[name="username"]', USERNAME);
      await page.fill('input[name="password"]', PASSWORD);
      await page.click('input[type="submit"]');
      await page.waitForURL((url) => !url.pathname.includes("/login"), {
        timeout: 10000,
      });
      return;
    } catch {
      if (i === retries) throw new Error("Login failed after retries");
      await page.waitForTimeout(1000);
    }
  }
}

test.describe("E4 証憑 E2EE", () => {
  test("暗号化 upload → サーバ保存 → fetch → 復号一致 + 一覧で復号表示", async ({ page, context }) => {
    test.setTimeout(60000);
    await login(page);
    await page.goto(`${BASE_URL}/vouchers/`);

    // 1) MK を worker に直接注入し、2) canvas サムネ生成 + 暗号化 upload、
    // 3) fetch + 復号して原画像一致を確認 — すべてページ内 (実 worker) で実行。
    const result = await page.evaluate(async (pngB64) => {
      let step = "import";
      try {
        const [sc, up, dl, thumb] = await Promise.all([
          import("/static/js/crypto/shared-client.js"),
          import("/static/js/crypto/voucher_upload.js"),
          import("/static/js/crypto/voucher_download.js"),
          import("/static/js/vouchers/thumbnail.js"),
        ]);
        step = "client+setKey";
        const client = new sc.SharedCryptoClient(
          "/static/js/crypto/shared-worker.js",
        );
        // client を window に退避して GC されないようにする (このページが
        // 開いている間 SharedWorker = MK を生かし続け、別ページの一覧描画が
        // 同じ MK で復号できるようにする)。
        window.__e2eVoucherClient = client;
        const key = new Uint8Array(32);
        crypto.getRandomValues(key);
        await client.setKey(key);
        const st = await client.status();
        if (!st.hasKey) return { error: "setKey failed (hasKey=false)" };

        step = "thumbnail";
        const params = JSON.parse(
          document.getElementById("vouchers-index-params").textContent,
        );
        const userId = params.user_id;
        const pngBytes = Uint8Array.from(atob(pngB64), (c) => c.charCodeAt(0));
        const file = new File([pngBytes], "receipt.png", { type: "image/png" });
        const t = await thumb.makeThumbnail(file);

        step = "upload";
        const res = await up.uploadEncryptedVoucher({
          client,
          userId,
          file,
          journalEntryId: null,
          makeThumbnail: () => Promise.resolve(t),
        });

        step = "download+decrypt";
        const back = await dl.fetchAndDecryptVoucherImage({
          client,
          userId,
          voucherId: res.voucherId,
        });
        const matches =
          back.length === pngBytes.length &&
          back.every((b, i) => b === pngBytes[i]);
        const thumbBack = await dl.fetchAndDecryptVoucherImage({
          client,
          userId,
          voucherId: res.voucherId,
          thumb: true,
        });

        return {
          ok: res.ok,
          voucherId: res.voucherId,
          cipherHash: res.file_hash_cipher,
          matches,
          thumbLen: thumbBack.length,
          thumbBytes: t.length,
        };
      } catch (e) {
        return { error: `[${step}] ${e && e.message ? e.message : String(e)}` };
      }
    }, PNG_B64);

    expect(result.error).toBeUndefined();
    expect(result.ok).toBe(true);
    expect(typeof result.voucherId).toBe("number");
    expect(result.cipherHash).toMatch(/^[0-9a-f]{64}$/);
    expect(result.matches).toBe(true); // 復号画像 == 原画像
    expect(result.thumbLen).toBeGreaterThan(0); // サムネも復号できる

    // 別ページ (同一 context) で証憑一覧を開く。元ページが SharedWorker (MK) を
    // 生かしているため、index_renderer が暗号化証憑を復号 <img> (blob: src) で
    // 表示できる。当該 voucher のサムネが blob: で出ることを確認。
    const page2 = await context.newPage();
    await page2.goto(`${BASE_URL}/vouchers/`);
    // 当該 voucher のカードを journal リンク or verify form の action から特定…
    // ここでは「blob: src の img が少なくとも 1 つ描画される」ことで復号表示の
    // グルーが機能していることを確認する (現行鍵で暗号化した voucher が対象)。
    await expect.poll(
      async () =>
        page2.locator('#vouchers-index-grid img[src^="blob:"]').count(),
      { timeout: 20000 },
    ).toBeGreaterThan(0);
    await page2.close();
  });
});
