// E2EE 鍵管理ウィザード E2E (#385 ログイン派生 MK 活性化版)。
//
// 活性化後はログイン (2 ラウンド + 透過セットアップ) 時点で MK と passphrase 由来の
// wrapped_key が確立済みになる。よってウィザードは「初回設定」ではなく
// 「登録済み鍵の管理 + 別方式 (リカバリ/Passkey) の追加」が主役になる。
// passphrase 単独方式は廃止 (= ログインパスワードに統合)。
//
// 注: 実ブラウザは SharedWorker を画面遷移後も保持するためログイン直後から MK は
// 解錠済みだが、Playwright(firefox) は遷移で SharedWorker を破棄するため、設定画面では
// 一旦ロック状態になる。よって本テストは「ログインパスワードで passphrase 鍵を解錠」
// してから管理操作を検証する (解錠の派生 = login_flow と同じ HKDF split)。

import { test, expect } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_test";
const PASSWORD = "e2e_pass_12345"; // gitleaks:allow E2E ダミー

async function login(page) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 30000 }),
    page.click('input[type="submit"]'),
  ]);
}

// 設定画面を開き、(Playwright で MK が消えていれば) ログインパスワードで解錠する。
async function openKeysUnlocked(page) {
  await page.goto(`${BASE_URL}/settings/encryption-keys`, { waitUntil: "networkidle" });
  const unlockBtn = page
    .locator("li.list-group-item", { hasText: "passphrase" })
    .locator('button:has-text("この鍵で解除")');
  if ((await unlockBtn.count()) > 0) {
    await unlockBtn.click();
    await page.fill('input[type="password"][autocomplete="current-password"]', PASSWORD);
    await page.click('button:has-text("解除する")');
    await expect(page.locator("text=解除済み")).toBeVisible({ timeout: 30000 });
  }
}

test.describe("暗号鍵管理ウィザード (#385 ログイン派生 MK)", () => {
  test.beforeEach(async ({ page }) => {
    test.setTimeout(60000);
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

  test("ログインで passphrase 鍵が確立済み・パスワードで解錠できる", async ({ page }) => {
    await openKeysUnlocked(page);
    // 解錠後は passphrase 鍵が 1 件・「別の方式を追加」が出る
    await expect(page.locator("ul.list-group > li")).toHaveCount(1);
    await expect(page.locator("li.list-group-item", { hasText: "passphrase" })).toBeVisible();
    await expect(page.locator("button", { hasText: "別の方式を追加" })).toBeVisible();
  });

  test("追加の方式選択はリカバリシードと Passkey の 2 つ (passphrase 単独は廃止)", async ({ page }) => {
    await openKeysUnlocked(page);
    await page.locator("button", { hasText: "別の方式を追加" }).click();
    await expect(page.locator("button", { hasText: "リカバリシード" })).toBeVisible();
    await expect(page.locator("button", { hasText: "Passkey" })).toBeVisible();
    await expect(page.locator("button", { hasText: "パスフレーズ" })).toHaveCount(0);
  });

  test("方式選択画面から戻るボタンで管理画面に戻る", async ({ page }) => {
    await openKeysUnlocked(page);
    await page.locator("button", { hasText: "別の方式を追加" }).click();
    await page.locator("button", { hasText: "戻る" }).click();
    await expect(page.locator("button", { hasText: "別の方式を追加" })).toBeVisible();
  });
});
