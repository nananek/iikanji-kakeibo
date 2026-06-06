// #385 PR-4: ログインパスワード変更の E2E (設計書 §3.3)。
//
// ログイン (透過移行で login 派生 MK 確立) → /settings/password で現在PW検証→MK再wrap→
// 新PW保存 → ログアウト → 新PWでログインできる / 旧PWでは失敗する、を端から端まで検証。
//
// パスワードを変更するため共有 e2e_test を汚染しないよう **専用ユーザー e2e_pwchange** を
// 毎回まっさらに seed する。

import { test, expect } from "@playwright/test";
import { spawnSync } from "child_process";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_pwchange";
const OLD_PW = "e2e_pass_12345"; // gitleaks:allow E2E ダミー
const NEW_PW = "e2e_newpass_67890"; // gitleaks:allow E2E ダミー

const SEED_SCRIPT = `
from app import create_app
from app.extensions import db
from app.models.user import User
from app.services.seed import seed_accounts_for_user
from app.services.account_deletion import delete_user_account
app = create_app()
with app.app_context():
    ex = User.query.filter_by(username='e2e_pwchange').first()
    if ex:
        delete_user_account(ex.id)
    u = User(username='e2e_pwchange', email='e2e_pwchange@test.local',
             user_type='personal')
    u.set_password('e2e_pass_12345')
    db.session.add(u)
    db.session.flush()
    seed_accounts_for_user(u.id)
    db.session.commit()
    print('PWCHANGE_UID=' + str(u.id))
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

async function login(page, password) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', password);
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 30000 }),
    page.click('input[type="submit"]'),
  ]);
}

test.describe("#385 ログインパスワード変更", () => {
  test.beforeEach(() => {
    const out = runPython(SEED_SCRIPT);
    if (!/PWCHANGE_UID=\d+/.test(out)) throw new Error("seed failed: " + out);
  });

  test("現在PWで変更 → 新PWでログイン可・旧PWは不可", async ({ page }) => {
    test.setTimeout(90000);
    // 初回ログイン (透過移行で login 派生 MK + passphrase 鍵を確立)
    await login(page, OLD_PW);

    // パスワード変更ページ
    await page.goto(`${BASE_URL}/settings/password`, { waitUntil: "networkidle" });
    await page.fill('input[name="current_password"]', OLD_PW);
    await page.fill('input[name="new_password"]', NEW_PW);
    await page.fill('input[name="new_password_confirm"]', NEW_PW);
    await page.click('button[type="submit"]');
    await expect(page.locator("#change-password-status"))
      .toHaveText("パスワードを変更しました。", { timeout: 30000 });

    // ログアウト → 新PWでログインできる
    await page.goto(`${BASE_URL}/logout`);
    await login(page, NEW_PW);
    expect(page.url()).not.toContain("/login");

    // ログアウト → 旧PWでは失敗する (ログイン画面に留まる)
    await page.goto(`${BASE_URL}/logout`);
    await page.goto(`${BASE_URL}/login`);
    await page.fill('input[name="username"]', USERNAME);
    await page.fill('input[name="password"]', OLD_PW);
    await page.click('input[type="submit"]');
    await expect(page.locator("#login-status")).toContainText("正しくありません", { timeout: 30000 });
    expect(page.url()).toContain("/login");
  });
});
