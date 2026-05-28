import { test, expect } from "@playwright/test";
import { spawnSync } from "child_process";

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
 * テスト用CSV（銀行明細風）
 * ヘッダー: 日付, 摘要, 入金, 出金
 */
function createTestCsv(): Buffer {
  const content = [
    "日付,摘要,入金,出金",
    "2026/01/20,テスト商店,,1500",
    "2026/01/21,給与振込,300000,",
  ].join("\n");
  return Buffer.from(content, "utf-8");
}

/**
 * 口座選択モーダルで科目を選択するヘルパー
 */
async function selectAccount(page, triggerSelector: string, accountName: string) {
  await page.click(triggerSelector);
  const modal = page.locator("#accountSelectorModal");
  await expect(modal).toBeVisible({ timeout: 5000 });
  const accountBtn = modal.locator("button.acct-item", { hasText: accountName }).first();
  await expect(accountBtn).toBeVisible({ timeout: 3000 });
  await accountBtn.click();
  await expect(modal).toBeHidden({ timeout: 5000 });
}

/**
 * CSVアップロード → 列マッピングページまで遷移するヘルパー
 */
async function navigateToMapping(page) {
  await page.goto(`${BASE_URL}/csv-import/`);
  // 段階的表示: 口座選択 → ファイル入力が出現 → 送信ボタンが出現
  await selectAccount(page, "#paymentAccountBtn", "現金");
  await page.locator('input[name="csv_file"]').setInputFiles({
    name: "test-bank.csv",
    mimeType: "text/csv",
    buffer: createTestCsv(),
  });
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/csv-import\/mapping/, { timeout: 15000 });
}

/**
 * 列マッピングページで列が未選択なら手動選択し、送信して確認ページへ遷移
 */
async function submitMappingAndGoToConfirm(page) {
  // AI検出 or 保存済みプロファイルで自動選択されていない場合に備えて手動設定
  const dateCol = page.locator('select[name="date_col"]');
  if (await dateCol.inputValue() === "") {
    await dateCol.selectOption("0");
  }
  const descCol = page.locator('select[name="desc_col"]');
  if (await descCol.inputValue() === "") {
    await descCol.selectOption("1");
  }
  const depositCol = page.locator('select[name="deposit_col"]');
  if (await depositCol.inputValue() === "") {
    await depositCol.selectOption("2");
  }
  const withdrawalCol = page.locator('select[name="withdrawal_col"]');
  if (await withdrawalCol.inputValue() === "") {
    await withdrawalCol.selectOption("3");
  }

  await page.click('button[type="submit"]');
  await page.waitForURL(/\/csv-import\/confirm/, { timeout: 15000 });
}

/**
 * テスト用のCSVプロファイルを削除
 */
function cleanupProfiles() {
  const script = `
from app import create_app
from app.extensions import db
from app.models.csv_column_profile import CsvColumnProfile
from app.models.user import User
app = create_app()
with app.app_context():
    u = User.query.filter_by(username='e2e_test').first()
    if u:
        CsvColumnProfile.query.filter_by(user_id=u.id).delete()
        db.session.commit()
    print('OK')
`;
  // stdin 経由で渡し、shell 補間を回避する
  const [cmd, ...args] = process.env.CI
    ? ["python", "-"]
    : ["docker", "compose", "exec", "-T", "web", "python", "-"];
  try {
    spawnSync(cmd, args, {
      input: script,
      encoding: "utf-8",
      timeout: 10000,
    });
  } catch {
    // ignore cleanup failures
  }
}

test.describe("CSV明細取込 — フルフロー", () => {
  test.beforeEach(async ({ page }) => {
    cleanupProfiles();
    await login(page);
  });

  // E3-F PR-A 以降、取込確定フローは MK 必須 (暗号化 batch POST)。
  // E2E テスト共通の MK 設定 (パスフレーズ wizard → unlock) が未整備のため、
  // 当面 skip。MK setup を共通化したらフォローアップ PR で再開する (Epic #220)。
  test.skip("CSVアップロード → 列マッピング(手動) → 確認 → 取込完了", async ({ page }) => {
    // Step 1: アップロード
    await navigateToMapping(page);

    // Step 2: 列マッピング — 初回はプロファイルも AI 自動検出も無いので未選択。
    // (E2-C-6d 以降、AI 列推定はユーザーがボタンを押した時のみクライアント
    // 完結で走る。サーバ自動検出は廃止されたためテストでは手動選択する。)
    // プレビューテーブルが表示されている
    await expect(page.locator("#previewTable")).toBeVisible();

    // 送信 → 確認ページ (submitMappingAndGoToConfirm 内で手動選択)
    await submitMappingAndGoToConfirm(page);

    // Step 3: 確認ページ — Alpine.js テーブルの行を待つ
    await expect(page.locator("h2")).toContainText("CSV明細取込");
    const rows = page.locator("#confirmTable tbody tr");
    await expect(rows).toHaveCount(2, { timeout: 10000 });

    // テスト商店（出金）が表示
    await expect(page.locator("#confirmTable")).toContainText("テスト商店");
    await expect(page.locator("#confirmTable")).toContainText("1,500");

    // 給与振込（入金）が表示
    await expect(page.locator("#confirmTable")).toContainText("給与振込");
    await expect(page.locator("#confirmTable")).toContainText("300,000");

    // ステータスがOK
    await expect(page.locator(".badge.bg-success").first()).toContainText("OK");

    // 取込ボタンをクリック
    const submitBtn = page.locator('button[type="submit"]', { hasText: "件を取り込む" });
    await expect(submitBtn).toBeVisible();
    await submitBtn.click();

    // 出納帳にリダイレクト & 成功トースト
    await page.waitForURL(/\/cashbook\//, { timeout: 10000 });
    await expect(page.locator(".toast-body")).toContainText("件を取り込みました", { timeout: 5000 });
  });

  // 上記と同様 (Epic #220 のフォローアップで MK 共通 fixture 整備後に再開)
  test.skip("2回目のCSV取込でプロファイルが復元される", async ({ page }) => {
    // --- 1回目: プロファイルを保存 ---
    await navigateToMapping(page);
    await submitMappingAndGoToConfirm(page);

    // 確認画面で取込実行
    const rows = page.locator("#confirmTable tbody tr");
    await expect(rows).toHaveCount(2, { timeout: 10000 });
    const submitBtn = page.locator('button[type="submit"]', { hasText: "件を取り込む" });
    await expect(submitBtn).toBeVisible();
    await submitBtn.click();
    await page.waitForURL(/\/cashbook\//, { timeout: 10000 });

    // --- 2回目: 同じ口座でCSV取込 ---
    await navigateToMapping(page);

    // 「保存済みプロファイル」バッジが表示される
    await expect(page.locator(".badge.bg-success")).toContainText("保存済みプロファイル");

    // 前回のマッピングが復元されている
    await expect(page.locator('select[name="date_col"]')).toHaveValue("0");
    await expect(page.locator('select[name="desc_col"]')).toHaveValue("1");
    await expect(page.locator('select[name="deposit_col"]')).toHaveValue("2");
    await expect(page.locator('select[name="withdrawal_col"]')).toHaveValue("3");
  });
});

test.describe("CSV明細取込 — 列マッピング画面", () => {
  test.beforeEach(async ({ page }) => {
    cleanupProfiles();
    await login(page);
  });

  test("プレビューハイライトが反映される", async ({ page }) => {
    await navigateToMapping(page);

    // 列を変更してハイライトが更新されるか
    await page.locator('select[name="date_col"]').selectOption("0");

    // ハイライトされた列がある
    const highlighted = page.locator("#previewTable .table-primary");
    await expect(highlighted.first()).toBeVisible();
  });
});

test.describe("CSV明細取込 — 確認画面", () => {
  test.beforeEach(async ({ page }) => {
    cleanupProfiles();
    await login(page);
    await navigateToMapping(page);
    await submitMappingAndGoToConfirm(page);
    // Alpine.js の初期化を待つ
    await page.locator("#confirmTable tbody tr").first().waitFor({ state: "visible", timeout: 10000 });
  });

  test("確認画面が正常に表示される（500エラー回帰テスト）", async ({ page }) => {
    await expect(page.locator("h2")).toContainText("CSV明細取込");
    await expect(page.locator("#confirmTable")).toBeVisible();
    const rows = page.locator("#confirmTable tbody tr");
    await expect(rows).toHaveCount(2);
  });

  test("全選択・全解除ボタンが動作する", async ({ page }) => {
    // 全解除
    await page.click('button:has-text("全解除")');
    const submitBtn = page.locator('button[type="submit"]', { hasText: "件を取り込む" });
    await expect(submitBtn).toContainText("0件を取り込む");

    // 全選択
    await page.click('button:has-text("全選択")');
    await expect(submitBtn).toContainText("2件を取り込む");
  });

  test("費目を変更できる", async ({ page }) => {
    // 1行目の費目ボタンをクリック
    const firstRow = page.locator("#confirmTable tbody tr").nth(0);
    const catBtn = firstRow.locator("td:nth-child(7) button");
    await catBtn.click();

    // 科目選択モーダルが表示される
    const modal = page.locator("#accountSelectorModal");
    await expect(modal).toBeVisible({ timeout: 5000 });

    // PLタブの可視科目を名前で選択（確認画面の費目選択はPLタブがアクティブ）
    const accountBtn = modal.locator("button.acct-item", { hasText: "食費" }).first();
    await expect(accountBtn).toBeVisible({ timeout: 3000 });
    await accountBtn.click();

    // モーダルが閉じる
    await expect(modal).toBeHidden({ timeout: 5000 });

    // 費目が変更されている
    const newCatText = await catBtn.locator("span").textContent();
    expect(newCatText?.trim()).toContain("食費");
  });
});
