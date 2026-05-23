// E2EE 鍵管理ウィザード E2E (E1 PR-F2)。
//
// 本テストはウィザード画面の **表示・遷移のみ** を検証。実際の鍵生成
// (Argon2id 派生・SharedWorker wrap・/api/v1/wrapped-keys POST) は
// hash-wasm CDN 読込が必要で CI 環境の外部ネットワーク制約 + 実 Argon2id
// が数秒かかるため、別途 PR-F2 後の手動検証 or PR-F3 統合 E2E で扱う。

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

test.describe("暗号鍵管理ウィザード", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("設定トップから暗号鍵管理カードを開ける", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/`);
    const card = page.locator("a", { hasText: "暗号鍵管理" });
    await expect(card).toBeVisible();
    await card.click();
    await expect(page).toHaveURL(/\/settings\/encryption-keys/);
    await expect(page.locator("h2")).toContainText("暗号鍵管理");
  });

  test("プレビュー警告バナーが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/encryption-keys`);
    await expect(page.locator(".alert-warning")).toContainText("プレビュー機能");
  });

  test("初期画面に「初回設定を開始」ボタンがある", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/encryption-keys`);
    const btn = page.locator("button", { hasText: "初回設定を開始" });
    await expect(btn).toBeVisible(); // Alpine init 後に表示される
  });

  test("方式選択画面に 2 つの選択肢がある", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/encryption-keys`);
    await page.locator("button", { hasText: "初回設定を開始" }).click();
    await expect(page.locator("button", { hasText: "パスフレーズ" })).toBeVisible();
    await expect(
      page.locator("button", { hasText: "リカバリシード" })
    ).toBeVisible();
  });

  test("パスフレーズ画面に入力フィールド 2 つが表示される", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/encryption-keys`);
    await page.locator("button", { hasText: "初回設定を開始" }).click();
    await page.locator("button", { hasText: "パスフレーズ" }).first().click();
    const pwInputs = page.locator('input[type="password"]');
    await expect(pwInputs).toHaveCount(2);
    // 短すぎる入力では submit でエラー表示 (派生処理に到達しない)
    await pwInputs.nth(0).fill("short");
    await pwInputs.nth(1).fill("short");
    await page.locator("button", { hasText: "登録する" }).click();
    await expect(page.locator(".alert-danger")).toContainText("8 文字以上");
  });

  test("方式選択画面から戻るボタンで開始画面に戻る", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings/encryption-keys`);
    await page.locator("button", { hasText: "初回設定を開始" }).click();
    await page.locator("button", { hasText: "戻る" }).click();
    await expect(page.locator("button", { hasText: "初回設定を開始" })).toBeVisible();
  });
});
