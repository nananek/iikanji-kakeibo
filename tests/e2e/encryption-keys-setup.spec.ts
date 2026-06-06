// 暗号鍵管理ウィザードの「実際の鍵生成・解錠」E2E (#385 ログイン派生 MK 活性化版)。
//
// 活性化後は MK と passphrase 鍵がログイン時に確立済みになる。本テストはその前提で
// ウィザードの中核機能 (SharedCryptoClient.generateKey/wrap/unwrap → createWrappedKey/
// deleteWrappedKey) がブラウザで正しく動くことを回帰検証する:
//   1. ログイン派生 passphrase 鍵をログインパスワードで解錠 (HKDF split 解錠)
//   2. リカバリシード方式を追加 (= 同じ MK を別方式で wrap)
//   3. ロック → 追加したリカバリシードで解錠 (= 同じ MK が復元できる)
//   4. passphrase 鍵を削除
//
// 本テストは passphrase 鍵を削除するため、共有 e2e_test ではなく **専用ユーザー
// e2e_keysetup** を毎回まっさらに seed して使う (他 spec の normal-path ログインが
// passphrase 鍵を必要とするため、共有ユーザーの鍵を消すと汚染する)。
//
// 注: Playwright(firefox) は画面遷移で SharedWorker を破棄するため、設定画面では
// 一旦ロック状態になる (実ブラウザは遷移後も保持)。よって解錠から始める。

import { test, expect } from "@playwright/test";
import { spawnSync } from "child_process";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_keysetup";
const PASSWORD = "e2e_pass_12345"; // gitleaks:allow E2E ダミー

const SEED_SCRIPT = `
from app import create_app
from app.extensions import db
from app.models.user import User
from app.services.seed import seed_accounts_for_user
from app.services.account_deletion import delete_user_account
app = create_app()
with app.app_context():
    ex = User.query.filter_by(username='e2e_keysetup').first()
    if ex:
        delete_user_account(ex.id)  # 鍵を毎回まっさらに (login 派生の前提)
    u = User(username='e2e_keysetup', email='e2e_keysetup@test.local',
             user_type='personal')
    u.set_password('e2e_pass_12345')
    db.session.add(u)
    db.session.flush()
    seed_accounts_for_user(u.id)
    db.session.commit()
    print('KEYSETUP_UID=' + str(u.id))
`;

function runPython(stdinScript: string, timeoutMs = 30000): string {
  const [cmd, ...args] = process.env.CI
    ? ["python", "-"]
    : ["docker", "compose", "exec", "-T", "web", "python", "-"];
  const result = spawnSync(cmd, args, {
    input: stdinScript, encoding: "utf-8", timeout: timeoutMs,
  });
  if (result.status !== 0) {
    throw new Error(`python failed (status=${result.status}): ${result.stderr}`);
  }
  return result.stdout;
}

async function login(page) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', PASSWORD);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 30000 }),
    page.click('input[type="submit"]'),
  ]);
}

async function openAndUnlock(page) {
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

test.describe("暗号鍵ウィザード: ログイン派生 MK の解錠・別方式追加 (回帰)", () => {
  test.beforeEach(() => {
    const out = runPython(SEED_SCRIPT);
    if (!/KEYSETUP_UID=\d+/.test(out)) throw new Error("seed failed: " + out);
  });

  test("ログインパスワードで解錠 → リカバリ追加 → リカバリで解錠 → passphrase 削除", async ({ page }) => {
    test.setTimeout(120000);
    await login(page);
    await openAndUnlock(page);
    // ログイン派生の passphrase 鍵が 1 件・解錠済み
    await expect(page.locator("ul.list-group > li")).toHaveCount(1);
    await expect(page.locator(".alert-danger:visible")).toHaveCount(0);

    // 1) リカバリシードを追加 (= 解錠中の MK を別方式で wrap)
    await page.click('button:has-text("別の方式を追加")');
    await page.click('button:has-text("リカバリシード")');
    const mnemonic = await page.evaluate(() => {
      const el = document.querySelector("[x-data]");
      return el && el._x_dataStack ? el._x_dataStack[0].mnemonic : "";
    });
    expect(mnemonic.split(" ").length).toBe(24);
    await page.check("#mnemonic-acked");
    await page.click('button:has-text("登録する")');
    await expect(page.locator("ul.list-group > li")).toHaveCount(2, { timeout: 30000 });
    await expect(page.locator(".alert-danger:visible")).toHaveCount(0);

    // 2) ロック → 追加したリカバリシードで解錠できる (= 同じ MK を wrap している)
    await page.click('button:has-text("今すぐロックする")');
    const recLi = page.locator("li.list-group-item", { hasText: "recovery_seed" });
    await recLi.locator('button:has-text("この鍵で解除")').click();
    await page.fill("textarea", mnemonic);
    await page.click('button:has-text("解除する")');
    await expect(page.locator("text=解除済み")).toBeVisible({ timeout: 30000 });

    // 3) passphrase 鍵を削除 → 1 件になる
    page.on("dialog", (d) => d.accept());
    const ppLi = page.locator("li.list-group-item", { hasText: "passphrase" });
    await ppLi.locator('button[title="この鍵を削除"]').click();
    await expect(page.locator("ul.list-group > li")).toHaveCount(1, { timeout: 30000 });
    await expect(page.locator(".alert-danger:visible")).toHaveCount(0);
  });
});
