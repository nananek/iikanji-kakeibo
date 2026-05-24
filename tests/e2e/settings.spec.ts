import { test, expect } from "@playwright/test";

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
    // 「通知」カードは auto_import 廃止 (PR #170) に伴い削除
    for (const label of [
      "勘定科目",
      "月次確定",
      "表示設定",
      "外部AI",
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

  test("表示設定カードから表示設定ページへ遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    await page.locator("a.card", { hasText: "表示設定" }).click();
    await expect(page).toHaveURL(/\/settings\/display/);
    await expect(page.locator("h2")).toContainText("表示設定");
  });
});

test.describe("表示設定ページ", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.goto(`${BASE_URL}/settings/display`);
  });

  test("レポートのデフォルト期間設定が表示される", async ({ page }) => {
    await expect(page.locator("h6", { hasText: "レポートのデフォルト期間" })).toBeVisible();
    await expect(page.locator("#period_all")).toBeVisible();
    await expect(page.locator("#period_current")).toBeVisible();
  });

  test("元帳のソート順設定が表示される", async ({ page }) => {
    await expect(page.locator("h6", { hasText: "元帳のソート順" })).toBeVisible();
    await expect(page.locator("#sort_asc")).toBeVisible();
    await expect(page.locator("#sort_desc")).toBeVisible();
  });

  test("デフォルト期間を当月に変更して保存できる", async ({ page }) => {
    await page.locator("#period_current").check();
    await page.locator('button[type="submit"]', { hasText: "保存" }).click();
    await expect(page).toHaveURL(/\/settings\/display/);
    await expect(page.locator(".toast")).toContainText("表示設定を保存しました");
    await expect(page.locator("#period_current")).toBeChecked();
  });

  test("ソート順を新しい順に変更して保存できる", async ({ page }) => {
    await page.locator("#sort_desc").check();
    await page.locator('button[type="submit"]', { hasText: "保存" }).click();
    await expect(page).toHaveURL(/\/settings\/display/);
    await expect(page.locator(".toast")).toContainText("表示設定を保存しました");
    await expect(page.locator("#sort_desc")).toBeChecked();
  });

  test("設定を元に戻せる（全期間・古い順）", async ({ page }) => {
    await page.locator("#period_all").check();
    await page.locator("#sort_asc").check();
    await page.locator('button[type="submit"]', { hasText: "保存" }).click();
    await expect(page.locator(".toast")).toContainText("表示設定を保存しました");
    await expect(page.locator("#period_all")).toBeChecked();
    await expect(page.locator("#sort_asc")).toBeChecked();
  });
});

test.describe("ヘッダーナビゲーション", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("設定リンクがドロップダウンではなく直接リンクである", async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    // ドロップダウンでないこと
    await expect(
      page.locator(".nav-link.dropdown-toggle", { hasText: "設定" })
    ).toHaveCount(0);
    // 直接リンクであること
    await expect(
      page.locator("a.nav-link", { hasText: "設定" })
    ).toBeVisible();
  });

  test("設定リンクから設定トップへ遷移", async ({ page }) => {
    await page.goto(`${BASE_URL}/`);
    await page.locator("a.nav-link", { hasText: "設定" }).click();
    await expect(page).toHaveURL(/\/settings\/$/);
  });
});
