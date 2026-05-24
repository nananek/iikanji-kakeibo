import { test, expect } from "@playwright/test";
import * as path from "path";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_test";
const PASSWORD = "e2e_pass_12345";

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

/**
 * テスト用の 1x1 白 JPEG を生成（Base64 デコード）
 */
function createTestJpeg(): Buffer {
  // 最小の有効な JPEG（1x1 白ピクセル）
  return Buffer.from(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP///////////////////////////" +
      "////////////////////////////////////////////////////////////" +
      "2wBDAf///////////////////////////////////////////////////////////" +
      "//////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/" +
      "EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AKwA=",
    "base64"
  );
}

test.describe("AI証憑仕訳 — アップロード〜レビュー", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("アップロードページが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/ai-journal/`);
    await expect(page.locator("h2")).toContainText("AI証憑仕訳");
  });

  // E2 PR-C-4e: サーバ AI 解析経路 (/ai-journal/analyze) を廃止し、E2EE
  // クライアント完結フローのみに統一。E2E 用 mock-ai-server は llama_cpp
  // 互換だが、クライアント側 LLM クライアント (round1.js/round2.js) は
  // OpenAI/Anthropic/Google のみ対応。E2-D で client-py / E2E 用クライアント
  // クライアント対応後に再有効化する。
  test.skip("画像アップロード〜AI解析〜レビュー画面遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/ai-journal/`);

    // ファイルアップロード
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: "test-receipt.jpg",
      mimeType: "image/jpeg",
      buffer: createTestJpeg(),
    });

    // 「AIで解析する」ボタンをクリック
    const analyzeBtn = page.locator("button", { hasText: "AIで解析" });
    await expect(analyzeBtn).toBeVisible();
    await analyzeBtn.click();

    // 解析完了を待つ（レビューボタンまたは一時保存ボタンが表示される）
    await page.waitForSelector("#suggestionsArea", {
      state: "visible",
      timeout: 15000,
    });

    // 仕訳案が表示される
    await expect(page.locator("#suggestionsArea")).toContainText("テスト商店");

    // レビューボタンをクリック
    const reviewBtn = page.locator("a", { hasText: "この仕訳を使う" }).first();
    await expect(reviewBtn).toBeVisible();
    await reviewBtn.click();

    // レビュー画面に遷移
    await page.waitForURL(/\/ai-journal\/review/);
    await expect(page.locator("h2")).toContainText("AI証憑仕訳");

    // かんたんモードに内容が表示される
    await expect(page.locator('input[name="date"]').first()).toHaveValue(
      "2026-01-15"
    );
    await expect(
      page.locator('input[name="description"]').first()
    ).toHaveValue("テスト商店");
  });
});

test.describe("AI証憑仕訳 — レビュー画面（下書きから）", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("下書き一覧からレビュー画面に遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/ai-journal/drafts`);

    // ドラフトが存在する
    const reviewLink = page.locator("a", { hasText: "編集" }).first();
    await expect(reviewLink).toBeVisible();
    await reviewLink.click();

    // レビュー画面に遷移
    await page.waitForURL(/\/ai-journal\/review/);
    await expect(page.locator("h2")).toContainText("AI証憑仕訳");
  });

  test("かんたんモードが初期表示される", async ({ page }) => {
    // 下書きのレビュー画面に直接遷移
    await page.goto(`${BASE_URL}/ai-journal/drafts`);
    await page.locator("a", { hasText: "編集" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // かんたんモードタブがアクティブ
    const simpleTab = page.locator("button", { hasText: "かんたんモード" });
    await expect(simpleTab).toHaveClass(/active/);

    // 日付と摘要が入っている
    const dateInput = page.locator("#simpleMode input[name='date']");
    await expect(dateInput).toHaveValue("2026-01-15");
  });
});

test.describe("AI証憑仕訳 — 仕訳モード（タブ切り替え）", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.goto(`${BASE_URL}/ai-journal/drafts`);
    await page.locator("a", { hasText: "編集" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);
  });

  test("仕訳モードタブに切り替え可能", async ({ page }) => {
    // 仕訳モードタブをクリック
    const advancedTab = page.locator("button", { hasText: "仕訳モード" });
    await advancedTab.click();

    // 仕訳モードが表示される
    const advancedPane = page.locator("#advancedMode");
    await expect(advancedPane).toBeVisible();

    // テーブルが表示される
    await expect(advancedPane.locator("table")).toBeVisible();
  });

  test("仕訳明細に勘定科目名が表示される", async ({ page }) => {
    // 仕訳モードに切り替え
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500); // Alpine 再レンダリング待ち

    const advancedPane = page.locator("#advancedMode");

    // 行が 2 行以上存在する
    const rows = advancedPane.locator("tbody tr");
    await expect(rows).toHaveCount(2);

    // 勘定科目名が表示されている（「-- 選択 --」ではない）
    const firstAccountBtn = rows.nth(0).locator("td:first-child button span");
    const firstText = await firstAccountBtn.textContent();
    expect(firstText).not.toBe("-- 選択 --");
    expect(firstText?.trim().length).toBeGreaterThan(0);

    const secondAccountBtn = rows.nth(1).locator("td:first-child button span");
    const secondText = await secondAccountBtn.textContent();
    expect(secondText).not.toBe("-- 選択 --");
  });

  test("仕訳明細に金額が入っている", async ({ page }) => {
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    const advancedPane = page.locator("#advancedMode");
    const rows = advancedPane.locator("tbody tr");

    // 借方 1500
    const debit = rows.nth(0).locator('input[type="number"]').first();
    await expect(debit).toHaveValue("1500");

    // 貸方 1500
    const credit = rows.nth(1).locator('input[type="number"]').nth(1);
    await expect(credit).toHaveValue("1500");
  });

  test("合計行に正しい金額が表示される", async ({ page }) => {
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    const advancedPane = page.locator("#advancedMode");
    const footer = advancedPane.locator("tfoot tr").first();

    // 合計が ¥1,500 であること
    await expect(footer.locator("th").nth(1)).toContainText("1,500");
    await expect(footer.locator("th").nth(2)).toContainText("1,500");
  });

  test("行を追加できる", async ({ page }) => {
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    const advancedPane = page.locator("#advancedMode");
    const rows = advancedPane.locator("tbody tr");

    // 初期 2 行
    await expect(rows).toHaveCount(2);

    // 「行を追加」ボタンをクリック
    await advancedPane.locator("button", { hasText: "行を追加" }).click();
    await expect(rows).toHaveCount(3);

    // 新しい行は空（「-- 選択 --」）
    const newRow = rows.nth(2);
    await expect(newRow.locator("td:first-child button span")).toHaveText(
      "-- 選択 --"
    );
  });

  test("行を削除できる", async ({ page }) => {
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    const advancedPane = page.locator("#advancedMode");
    const rows = advancedPane.locator("tbody tr");

    await expect(rows).toHaveCount(2);

    // 最初の行の削除ボタンをクリック
    await rows.nth(0).locator("button.btn-outline-danger").click();
    await expect(rows).toHaveCount(1);
  });

  test("貸借不一致時に警告が表示される", async ({ page }) => {
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    const advancedPane = page.locator("#advancedMode");

    // 借方金額を変更して不一致にする
    const debit = advancedPane
      .locator("tbody tr")
      .nth(0)
      .locator('input[type="number"]')
      .first();
    await debit.fill("9999");

    // 不一致警告が表示される
    await expect(
      advancedPane.locator("text=貸借が一致していません")
    ).toBeVisible();
  });

  test("日付と摘要が表示される", async ({ page }) => {
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    const advancedPane = page.locator("#advancedMode");

    // 日付
    await expect(advancedPane.locator('input[name="date"]')).toHaveValue(
      "2026-01-15"
    );

    // 摘要
    await expect(
      advancedPane.locator('input[name="description"]')
    ).toHaveValue("テスト商店");
  });
});

test.describe("AI証憑仕訳 — 仕訳モードからの登録", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // E2 PR-C-4e: サーバ AI 解析経路廃止に伴い skip (E2-D で再有効化)
  test.skip("仕訳モードで仕訳を登録できる", async ({ page }) => {
    // アップロード → 解析（mock AI）
    await page.goto(`${BASE_URL}/ai-journal/`);
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: "test-receipt.jpg",
      mimeType: "image/jpeg",
      buffer: createTestJpeg(),
    });
    await page.locator("button", { hasText: "AIで解析" }).click();
    await page.waitForSelector("#suggestionsArea", {
      state: "visible",
      timeout: 15000,
    });
    await page.locator("a", { hasText: "この仕訳を使う" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // 仕訳モードに切り替え
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    // 「仕訳を登録」をクリック
    const submitBtn = page
      .locator("#advancedMode")
      .locator("button", { hasText: "仕訳を登録" });
    await submitBtn.click();

    // 仕訳帳にリダイレクト（temp ドラフトなので /journal/ へ）
    await page.waitForURL(/\/journal\/($|\?)/, { timeout: 10000 });
  });
});

test.describe("AI証憑仕訳 — かんたんモードからの登録", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // E2 PR-C-4e: サーバ AI 解析経路廃止に伴い skip (E2-D で再有効化)
  test.skip("かんたんモードで仕訳を登録でき日付が正しく入る", async ({ page }) => {
    // アップロード → 解析（mock AI）
    await page.goto(`${BASE_URL}/ai-journal/`);
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: "test-receipt.jpg",
      mimeType: "image/jpeg",
      buffer: createTestJpeg(),
    });
    await page.locator("button", { hasText: "AIで解析" }).click();
    await page.waitForSelector("#suggestionsArea", {
      state: "visible",
      timeout: 15000,
    });
    await page.locator("a", { hasText: "この仕訳を使う" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // かんたんモードの日付を確認
    const dateInput = page.locator("#simpleMode input[name='date']");
    await expect(dateInput).toHaveValue("2026-01-15");

    // 「仕訳を登録」をクリック
    const submitBtn = page
      .locator("#simpleMode")
      .locator("button", { hasText: "仕訳を登録" });
    await submitBtn.click();

    // 仕訳帳にリダイレクト
    await page.waitForURL(/\/journal\/($|\?)/, { timeout: 10000 });

    // 仕訳帳に日付 2026/01/15 の仕訳が表示されている
    await expect(page.locator("table")).toContainText("2026/01/15");
    await expect(page.locator("table")).toContainText("テスト商店");
  });
});

test.describe("AI証憑仕訳 — 仕訳モード登録後の日付確認", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // E2 PR-C-4e: サーバ AI 解析経路廃止に伴い skip (E2-D で再有効化)
  test.skip("仕訳モードで登録した仕訳に日付が正しく入る", async ({ page }) => {
    // アップロード → 解析（mock AI）
    await page.goto(`${BASE_URL}/ai-journal/`);
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: "test-receipt.jpg",
      mimeType: "image/jpeg",
      buffer: createTestJpeg(),
    });
    await page.locator("button", { hasText: "AIで解析" }).click();
    await page.waitForSelector("#suggestionsArea", {
      state: "visible",
      timeout: 15000,
    });
    await page.locator("a", { hasText: "この仕訳を使う" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // 仕訳モードに切り替え
    await page.locator("button", { hasText: "仕訳モード" }).click();
    await page.waitForTimeout(500);

    // 仕訳モードの日付を確認
    const dateInput = page.locator("#advancedMode input[name='date']");
    await expect(dateInput).toHaveValue("2026-01-15");

    // 「仕訳を登録」をクリック
    const submitBtn = page
      .locator("#advancedMode")
      .locator("button", { hasText: "仕訳を登録" });
    await submitBtn.click();

    // 仕訳帳にリダイレクト
    await page.waitForURL(/\/journal\/($|\?)/, { timeout: 10000 });

    // 仕訳帳に日付 2026/01/15 の仕訳が表示されている
    await expect(page.locator("table")).toContainText("2026/01/15");
    await expect(page.locator("table")).toContainText("テスト商店");
  });
});

test.describe("AI証憑仕訳 — 下書き保存・やり直し確認", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // E2 PR-C-4e: サーバ AI 解析経路廃止に伴い skip (E2-D で再有効化)
  test.skip("temp ドラフトに下書き保存ボタンが表示される", async ({ page }) => {
    // アップロード → 解析
    await page.goto(`${BASE_URL}/ai-journal/`);
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: "test-receipt.jpg",
      mimeType: "image/jpeg",
      buffer: createTestJpeg(),
    });
    await page.locator("button", { hasText: "AIで解析" }).click();
    await page.waitForSelector("#suggestionsArea", {
      state: "visible",
      timeout: 15000,
    });
    await page.locator("a", { hasText: "この仕訳を使う" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // 下書き保存ボタンが表示される
    await expect(
      page.locator("button", { hasText: "下書き保存" }).first()
    ).toBeVisible();

    // やり直すボタンにも確認ダイアログが付いている
    const retryBtn = page.locator("a", { hasText: "やり直す" }).first();
    await expect(retryBtn).toBeVisible();
    const onclick = await retryBtn.getAttribute("onclick");
    expect(onclick).toContain("confirm(");
  });

  test("saved ドラフトには一覧に戻るボタンが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/ai-journal/drafts`);
    await page.locator("a", { hasText: "編集" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // 「一覧に戻る」ボタンが表示される
    await expect(
      page.locator("a", { hasText: "一覧に戻る" }).first()
    ).toBeVisible();

    // 「下書き保存」ボタンは表示されない
    await expect(
      page.locator("button", { hasText: "下書き保存" })
    ).toHaveCount(0);
  });
});

test.describe("仕訳編集画面 — tojson属性バグ", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  // E2 PR-C-4e: サーバ AI 解析経路廃止に伴い skip (E2-D で再有効化)
  test.skip("証憑から開いた仕訳編集画面で日付・明細が表示される", async ({ page }) => {
    // まず仕訳を登録（AI証憑仕訳経由）
    await page.goto(`${BASE_URL}/ai-journal/`);
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles({
      name: "test-receipt.jpg",
      mimeType: "image/jpeg",
      buffer: createTestJpeg(),
    });
    await page.locator("button", { hasText: "AIで解析" }).click();
    await page.waitForSelector("#suggestionsArea", {
      state: "visible",
      timeout: 15000,
    });
    await page.locator("a", { hasText: "この仕訳を使う" }).first().click();
    await page.waitForURL(/\/ai-journal\/review/);

    // かんたんモードで登録
    await page
      .locator("#simpleMode")
      .locator("button", { hasText: "仕訳を登録" })
      .click();
    await page.waitForURL(/\/journal\/($|\?)/, { timeout: 10000 });

    // 日付と摘要でフィルターして目的の仕訳を表示
    await page.goto(`${BASE_URL}/journal/?date_from=2026-01-15&date_to=2026-01-15&search=%E3%83%86%E3%82%B9%E3%83%88%E5%95%86%E5%BA%97`);
    const editLink = page.locator("table a[href*='/edit']").first();
    await expect(editLink).toBeVisible();
    await editLink.click();
    await page.waitForURL(/\/(journal|cashbook)\/\d+\/edit/);

    // 日付が入っている
    const dateInput = page.locator("input[name='date']");
    await expect(dateInput).toHaveValue("2026-01-15");

    // 仕訳明細が表示されている（2行以上）
    const rows = page.locator("#journalForm tbody tr");
    const count = await rows.count();
    expect(count).toBeGreaterThanOrEqual(2);

    // 勘定科目名が表示されている（「-- 選択 --」ではない）
    const firstAccountBtn = rows.nth(0).locator("td:first-child button span");
    const firstText = await firstAccountBtn.textContent();
    expect(firstText).not.toBe("-- 選択 --");
    expect(firstText?.trim().length).toBeGreaterThan(0);

    // 金額が入っている
    const debit = rows.nth(0).locator('input[type="number"]').first();
    await expect(debit).toHaveValue("1500");
  });
});
