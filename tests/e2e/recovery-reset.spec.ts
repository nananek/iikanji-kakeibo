// #385 PR-4b-3: リカバリシードによるパスワードリセットの E2E (設計書 §3.4.1)。
//
// ログイン (login 派生 MK 確立) → ウィザードでリカバリシード追加 (recovery_seed_server_hash
// 確立、24 語を捕捉) → ログアウト → /auth/recovery-reset でシード + 新パスワードを入力 →
// 新シードがセキュア表示される → 新パスワードでログインできる / 旧パスワードは不可、を
// 端から端まで検証する。
//
// 専用ユーザー e2e_reset を毎回まっさらに seed する (鍵を消すため共有 e2e_test を汚染しない)。

import { test, expect } from "@playwright/test";
import { runPython } from "./helpers";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_reset";
const OLD_PW = "e2e_pass_12345";     // gitleaks:allow E2E ダミー
const NEW_PW = "e2e_reset_67890";    // gitleaks:allow E2E ダミー

const SEED_SCRIPT = `
from app import create_app
from app.extensions import db
from app.models.user import User
from app.services.seed import seed_accounts_for_user
from app.services.account_deletion import delete_user_account
app = create_app()
with app.app_context():
    ex = User.query.filter_by(username='e2e_reset').first()
    if ex:
        delete_user_account(ex.id)
    u = User(username='e2e_reset', email='e2e_reset@test.local', user_type='personal')
    u.set_password('e2e_pass_12345')
    db.session.add(u)
    db.session.flush()
    seed_accounts_for_user(u.id)
    db.session.commit()
    print('RESET_UID=' + str(u.id))
`;


async function login(page, password) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', password);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 30000 }),
    page.click('input[type="submit"]'),
  ]);
}

test.describe("#385 リカバリシードでパスワードリセット", () => {
  test.beforeEach(() => {
    const out = runPython(SEED_SCRIPT);
    if (!/RESET_UID=\d+/.test(out)) throw new Error("seed failed: " + out);
  });

  test("シード追加 → リセット → 新シード表示 → 新PWでログイン可・旧PWは不可", async ({ page }) => {
    test.setTimeout(150000);

    // 1) 初回ログイン (login 派生 MK + passphrase 鍵を確立)
    await login(page, OLD_PW);

    // 2) ウィザードでリカバリシードを追加 (recovery_seed_server_hash を確立) し 24 語を捕捉
    await page.goto(`${BASE_URL}/settings/encryption-keys`, { waitUntil: "networkidle" });
    const unlockBtn = page
      .locator("li.list-group-item", { hasText: "passphrase" })
      .locator('button:has-text("この鍵で解除")');
    if ((await unlockBtn.count()) > 0) {
      await unlockBtn.click();
      await page.fill('input[type="password"][autocomplete="current-password"]', OLD_PW);
      await page.click('button:has-text("解除する")');
      await expect(page.locator("text=解除済み")).toBeVisible({ timeout: 30000 });
    }
    await page.click('button:has-text("別の方式を追加")');
    await page.click('button:has-text("リカバリシード")');
    const mnemonic = await page.evaluate(() => {
      const el = document.querySelector("[x-data]");
      return el && (el as any)._x_dataStack ? (el as any)._x_dataStack[0].mnemonic : "";
    });
    expect(mnemonic.split(" ").length).toBe(24);
    await page.check("#mnemonic-acked");
    await page.click('button:has-text("登録する")');
    await expect(page.locator("ul.list-group > li")).toHaveCount(2, { timeout: 30000 });

    // 3) ログアウト
    await page.goto(`${BASE_URL}/logout`);

    // 4) リセットページで username + 旧シード + 新パスワードを入力
    await page.goto(`${BASE_URL}/auth/recovery-reset`, { waitUntil: "networkidle" });
    await page.fill('input[name="username"]', USERNAME);
    await page.fill('textarea[name="mnemonic"]', mnemonic);
    await page.fill('input[name="new_password"]', NEW_PW);
    await page.fill('input[name="new_password_confirm"]', NEW_PW);
    await page.click('button:has-text("パスワードをリセットする")');

    // 5) 新シードがセキュア表示される (完了カード + 24 語)
    await expect(page.locator("#reset-done-card")).toBeVisible({ timeout: 60000 });
    const newWords = await page.locator("#new-seed-grid > div").count();
    expect(newWords).toBe(24);

    // 6) 新パスワードでログインできる
    await page.goto(`${BASE_URL}/logout`);
    await login(page, NEW_PW);
    expect(page.url()).not.toContain("/login");

    // 7) 旧パスワードでは失敗する
    await page.goto(`${BASE_URL}/logout`);
    await page.goto(`${BASE_URL}/login`);
    await page.fill('input[name="username"]', USERNAME);
    await page.fill('input[name="password"]', OLD_PW);
    await page.click('input[type="submit"]');
    await expect(page.locator("#login-status")).toContainText("正しくありません", { timeout: 30000 });
    expect(page.url()).toContain("/login");
  });
});
