import { test, expect, Page } from "@playwright/test";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_test";
const PASSWORD = "e2e_pass_12345";
// global-setup.ts で作成する確定済み期間（2025年）の伝票
const LOCKED_DESC = "E2E確定済仕訳";

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

test.describe("元帳: 確定済み伝票の複写", () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
  });

  test("確定済み伝票を複写すると仕訳行を編集できる", async ({ page }) => {
    // 2025年（確定済み）の先頭科目（現金）の元帳を開く
    await page.goto(`${BASE_URL}/reports/ledger`);
    const firstOption = page.locator("#accountSelect option[data-code]").first();
    const accountCode = await firstOption.getAttribute("value");
    expect(accountCode).toBeTruthy();
    await page.goto(
      `${BASE_URL}/reports/ledger?year=2025&pf=0&pt=15&account_code=${accountCode}`
    );

    // 確定済み伝票の行を開く（閲覧モード）
    const row = page.locator("tbody tr", { hasText: LOCKED_DESC });
    await expect(row).toHaveCount(1);
    await row.locator('button[data-action="openEditModal"]').click();
    await page.waitForSelector("#editModal.show");

    // 確定済み伝票は閲覧モード（保存ボタン非表示）
    await expect(page.locator("#modalSaveBtn")).toBeHidden();

    // 複写をクリック
    await page.locator("#modalCopyBtn").click();

    // 明細行が編集可能になっていること
    // 1) 勘定科目を選択できる
    await expect(
      page.locator("#modalLinesBody .modal-account-btn").first()
    ).toBeVisible();

    // 2) 金額を編集できる
    const debit = page.locator("#modalLinesBody .modal-debit").first();
    await expect(debit).toBeEditable();
    await debit.fill("3000");
    await expect(debit).toHaveValue("3000");

    // 3) 行を削除できる
    await expect(
      page.locator("#modalLinesBody .modal-remove-btn").first()
    ).toBeVisible();
  });
});
