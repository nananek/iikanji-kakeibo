import { test, expect } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_test";
const PASSWORD = "e2e_pass_12345";

async function login(page) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('input[type="submit"]');
  await page.waitForURL((url) => !url.pathname.includes("/login"));
}

test.describe("設定トップページ", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("設定トップページが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    await expect(page.locator("h2")).toContainText("設定");
  });

  test("3つのカテゴリーが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    await expect(page.locator("h5").filter({ hasText: "帳簿" })).toBeVisible();
    await expect(
      page.locator("h5").filter({ hasText: "AI・連携" })
    ).toBeVisible();
    await expect(
      page.locator("h5").filter({ hasText: "セキュリティ" })
    ).toBeVisible();
  });

  test("全設定カードが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    for (const label of [
      "勘定科目",
      "月次確定",
      "AI API設定",
      "自動取込",
      "APIキー管理",
      "Passkey管理",
    ]) {
      await expect(page.locator("a.card", { hasText: label })).toBeVisible();
    }
  });

  test("勘定科目カードから勘定科目ページへ遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    await page.locator("a.card", { hasText: "勘定科目" }).click();
    await expect(page).toHaveURL(/\/accounts\//);
  });

  test("月次確定カードから月次確定ページへ遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    await page.locator("a.card", { hasText: "月次確定" }).click();
    await expect(page).toHaveURL(/\/settings\/fiscal/);
  });

  test("personalユーザーに監査アクセス管理が表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    await expect(
      page.locator("a.card", { hasText: "監査アクセス管理" })
    ).toBeVisible();
  });
});

test.describe("ヘッダーナビゲーション", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("設定ドロップダウンにカテゴリーが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await page.locator(".nav-link.dropdown-toggle", { hasText: "設定" }).click();

    await expect(
      page.locator(".dropdown-item", { hasText: "設定トップ" })
    ).toBeVisible();
    await expect(
      page.locator(".dropdown-header", { hasText: "帳簿" })
    ).toBeVisible();
    await expect(
      page.locator(".dropdown-header", { hasText: "AI・連携" })
    ).toBeVisible();
    await expect(
      page.locator(".dropdown-header", { hasText: "セキュリティ" })
    ).toBeVisible();
  });

  test("設定ドロップダウンに勘定科目がある", async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await page.locator(".nav-link.dropdown-toggle", { hasText: "設定" }).click();
    await expect(
      page.locator(".dropdown-item", { hasText: "勘定科目" })
    ).toBeVisible();
  });

  test("トップナビに勘定科目の独立リンクがない", async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    // ドロップダウン外の直接ナビリンクに「勘定科目」がないこと
    const directLink = page.locator(
      ".navbar-nav > .nav-item > a.nav-link:not(.dropdown-toggle)",
      { hasText: "勘定科目" }
    );
    await expect(directLink).toHaveCount(0);
  });

  test("設定トップから設定ページへ遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await page.locator(".nav-link.dropdown-toggle", { hasText: "設定" }).click();
    await page.locator(".dropdown-item", { hasText: "設定トップ" }).click();
    await expect(page).toHaveURL(/\/settings\/$/);
  });
});
