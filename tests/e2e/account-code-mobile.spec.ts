import { test, expect, Page } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_test";
const PASSWORD = "e2e_pass_12345";

const MOBILE_WIDTH = 375;
const MOBILE_HEIGHT = 667;
const DESKTOP_WIDTH = 1280;
const DESKTOP_HEIGHT = 800;

async function login(page: Page) {
  for (let i = 0; i < 3; i++) {
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
      if (i === 2) throw new Error("Login failed after retries");
      await page.waitForTimeout(1000);
    }
  }
}

// 科目コードのパターン（4桁数字）
const CODE_PATTERN = /^\d{4}$/;

test.describe("モバイル: 科目コードが非表示", () => {
  test.use({ viewport: { width: MOBILE_WIDTH, height: MOBILE_HEIGHT } });

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("勘定科目管理 — コード列が非表示", async ({ page }) => {
    await page.goto(`${BASE_URL}/accounts/`);
    await expect(page.locator("h2")).toContainText("勘定科目");

    // 各テーブルのヘッダー「コード」が非表示（複数テーブルあるので全てチェック）
    const codeHeaders = page.locator("th.d-mobile-none").filter({ hasText: "コード" });
    const headerCount = await codeHeaders.count();
    expect(headerCount).toBeGreaterThan(0);
    for (let i = 0; i < headerCount; i++) {
      await expect(codeHeaders.nth(i)).toBeHidden();
    }

    // データ行のコード列も非表示
    const codeCells = page.locator("td.d-mobile-none");
    const cellCount = await codeCells.count();
    expect(cellCount).toBeGreaterThan(0);
    for (let i = 0; i < Math.min(cellCount, 3); i++) {
      await expect(codeCells.nth(i)).toBeHidden();
    }
  });

  test("残高試算表 — コード列が非表示", async ({ page }) => {
    await page.goto(`${BASE_URL}/reports/balance`);
    await expect(page.locator("h2")).toContainText("残高試算表");

    const codeHeader = page.locator("th.d-mobile-none").filter({ hasText: "コード" });
    await expect(codeHeader).toBeHidden();
  });

  test("総勘定元帳 — 科目selectにコードが含まれない", async ({ page }) => {
    await page.goto(`${BASE_URL}/reports/ledger`);
    await expect(page.locator("h2")).toContainText("総勘定元帳");

    // selectのoption内テキストにコードが含まれないことを確認
    const options = page.locator("#accountSelect option[data-code]");
    const count = await options.count();
    expect(count).toBeGreaterThan(0);
    for (let i = 0; i < Math.min(count, 5); i++) {
      const text = await options.nth(i).textContent();
      // テキストがコードで始まらない
      expect(text?.trim()).not.toMatch(/^\d{4}\s/);
    }
  });

  test("総勘定元帳 — カードヘッダーの科目コードが非表示", async ({ page }) => {
    // 最初の科目を選択
    await page.goto(`${BASE_URL}/reports/ledger`);
    const firstOption = page.locator("#accountSelect option[data-code]").first();
    const optionValue = await firstOption.getAttribute("value");
    if (optionValue) {
      await page.locator("#accountSelect").selectOption(optionValue);
      await page.waitForURL(/account_id=/);

      // カードヘッダー内のコード部分が非表示
      const codeSpan = page.locator(".card-header .d-mobile-none").first();
      await expect(codeSpan).toBeHidden();
    }
  });

  test("科目選択モーダル — コードが非表示", async ({ page }) => {
    // 仕訳帳入力でモーダルを開く
    await page.goto(`${BASE_URL}/journal/new`);
    // 科目選択ボタンをクリック
    const selectBtn = page.locator("button.form-control").first();
    await selectBtn.click();

    // モーダルが開くのを待つ
    await page.waitForSelector("#accountSelectorModal.show", { timeout: 5000 });

    // モーダル内のコード表示が非表示
    const codeSmall = page.locator("#accountSelectorModal small.d-mobile-none").first();
    await expect(codeSmall).toBeHidden();
  });

  test("科目ピッカー選択後 — ボタンにコードが含まれない", async ({ page }) => {
    await page.goto(`${BASE_URL}/journal/new`);
    const selectBtn = page.locator("button.form-control").first();
    await selectBtn.click();
    await page.waitForSelector("#accountSelectorModal.show", { timeout: 5000 });

    // 最初の科目を選択
    const firstItem = page.locator("#accountSelectorModal .acct-item").first();
    await firstItem.click();

    // ボタンのテキストにコードが含まれない
    const btnText = await selectBtn.textContent();
    expect(btnText?.trim()).not.toMatch(/^\d{4}\s/);
  });
});

test.describe("デスクトップ: 科目コードが表示", () => {
  test.use({ viewport: { width: DESKTOP_WIDTH, height: DESKTOP_HEIGHT } });

  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("勘定科目管理 — コード列が表示", async ({ page }) => {
    await page.goto(`${BASE_URL}/accounts/`);
    await expect(page.locator("h2")).toContainText("勘定科目");

    // 最初のテーブルのヘッダー「コード」が表示
    const codeHeader = page.locator("th.d-mobile-none").filter({ hasText: "コード" }).first();
    await expect(codeHeader).toBeVisible();

    // データ行にコードが表示される
    const firstCodeCell = page.locator("td.d-mobile-none").first();
    await expect(firstCodeCell).toBeVisible();
    const text = await firstCodeCell.textContent();
    expect(text?.trim()).toMatch(CODE_PATTERN);
  });

  test("残高試算表 — コード列が表示", async ({ page }) => {
    await page.goto(`${BASE_URL}/reports/balance`);
    await expect(page.locator("h2")).toContainText("残高試算表");

    const codeHeader = page.locator("th.d-mobile-none").filter({ hasText: "コード" });
    await expect(codeHeader).toBeVisible();
  });

  test("総勘定元帳 — 科目selectにコードが含まれる", async ({ page }) => {
    await page.goto(`${BASE_URL}/reports/ledger`);

    const options = page.locator("#accountSelect option[data-code]");
    const count = await options.count();
    expect(count).toBeGreaterThan(0);
    // デスクトップではJSでコードが付与される
    const text = await options.first().textContent();
    expect(text?.trim()).toMatch(/^\d{4}\s/);
  });

  test("総勘定元帳 — カードヘッダーに科目コードが表示", async ({ page }) => {
    await page.goto(`${BASE_URL}/reports/ledger`);
    const firstOption = page.locator("#accountSelect option[data-code]").first();
    const optionValue = await firstOption.getAttribute("value");
    if (optionValue) {
      await page.locator("#accountSelect").selectOption(optionValue);
      await page.waitForURL(/account_id=/);

      const codeSpan = page.locator(".card-header .d-mobile-none").first();
      await expect(codeSpan).toBeVisible();
      const text = await codeSpan.textContent();
      expect(text?.trim()).toMatch(CODE_PATTERN);
    }
  });

  test("科目選択モーダル — コードが表示", async ({ page }) => {
    await page.goto(`${BASE_URL}/journal/new`);
    const selectBtn = page.locator("button.form-control").first();
    await selectBtn.click();

    await page.waitForSelector("#accountSelectorModal.show", { timeout: 5000 });

    const codeSmall = page.locator("#accountSelectorModal small.d-mobile-none").first();
    await expect(codeSmall).toBeVisible();
    const text = await codeSmall.textContent();
    expect(text?.trim()).toMatch(CODE_PATTERN);
  });

  test("科目ピッカー選択後 — ボタンにコードが含まれる", async ({ page }) => {
    await page.goto(`${BASE_URL}/journal/new`);
    const selectBtn = page.locator("button.form-control").first();
    await selectBtn.click();
    await page.waitForSelector("#accountSelectorModal.show", { timeout: 5000 });

    // 最初の科目を選択
    const firstItem = page.locator("#accountSelectorModal .acct-item").first();
    await firstItem.click();

    // ボタンのテキストにコードが含まれる
    const btnText = await selectBtn.textContent();
    expect(btnText?.trim()).toMatch(/^\d{4}\s/);
  });
});
