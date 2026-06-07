// #385 PR-T3: TOTP 2FA ログイン統合の E2E (設計書 §3.6.4)。
//
// ログイン (MK 確立) → 設定で TOTP 有効化 (QR の手動キーから現在コードを算出して確認) →
// ログアウト → 再ログインで password → totp_required → コード入力 → ログイン成功、を
// 端から端まで検証する。
//
// 専用ユーザー e2e_totp を毎回まっさらに seed する。

import { test, expect } from "@playwright/test";
import { runPython } from "./helpers";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_totp";
const PASSWORD = "e2e_pass_12345"; // gitleaks:allow E2E ダミー

const SEED_SCRIPT = `
from app import create_app
from app.extensions import db
from app.models.user import User
from app.services.seed import seed_accounts_for_user
from app.services.account_deletion import delete_user_account
app = create_app()
with app.app_context():
    ex = User.query.filter_by(username='e2e_totp').first()
    if ex:
        delete_user_account(ex.id)
    u = User(username='e2e_totp', email='e2e_totp@test.local', user_type='personal')
    u.set_password('e2e_pass_12345')
    db.session.add(u); db.session.flush()
    seed_accounts_for_user(u.id)
    db.session.commit()
    print('TOTP_UID=' + str(u.id))
`;


function totpNow(base32: string): string {
  const out = runPython(`import pyotp; print(pyotp.TOTP(${JSON.stringify(base32)}).now())`);
  const m = out.match(/\d{6}/);
  if (!m) throw new Error("could not compute TOTP: " + out);
  return m[0];
}

function resetLastUsedStep() {
  // confirm で記録された totp_last_used_step をクリアし、同一 step コードの replay 判定で
  // ログインがブロックされないようにする (新しい時間窓に入った状況を再現)。
  runPython(`
from app import create_app
from app.extensions import db
from app.models.user import User
app = create_app()
with app.app_context():
    u = User.query.filter_by(username='e2e_totp').first()
    u.totp_last_used_step = None
    db.session.commit()
`);
}

async function login(page, totpCode?: string) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', PASSWORD);
  await page.click('input[type="submit"]');
  if (totpCode) {
    // totp_required で totp-section が表示されるのを待ち、コードを入れて再 submit。
    await expect(page.locator("#totp-section")).toBeVisible({ timeout: 30000 });
    await page.fill("#totp-code", totpCode);
    await Promise.all([
      page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 30000 }),
      page.click('input[type="submit"]'),
    ]);
  }
}

test.describe("#385 TOTP 2FA ログイン", () => {
  test.beforeEach(() => {
    const out = runPython(SEED_SCRIPT);
    if (!/TOTP_UID=\d+/.test(out)) throw new Error("seed failed: " + out);
  });

  test("TOTP 有効化 → ログアウト → password+TOTP でログイン", async ({ page }) => {
    test.setTimeout(150000);

    // 1) 初回ログイン (MK 確立)
    await login(page);
    await expect(page).toHaveURL((u) => !u.pathname.includes("/login"), { timeout: 30000 });

    // 2) TOTP を有効化
    await page.goto(`${BASE_URL}/settings/totp`, { waitUntil: "networkidle" });
    await page.click('button:has-text("TOTP を有効化する")');
    await expect(page.locator("#totp-manual")).toBeVisible({ timeout: 30000 });
    const secret = (await page.locator("#totp-manual").textContent())?.trim() || "";
    expect(secret.length).toBeGreaterThan(0);
    await page.fill('input[name="code"]', totpNow(secret));
    await page.click('button:has-text("確認して有効化")');
    // バックアップコード表示 = 有効化成功
    await expect(page.locator("#backup-codes")).toBeVisible({ timeout: 30000 });

    // 3) replay 回避のため last_used_step をリセット → ログアウト
    resetLastUsedStep();
    await page.goto(`${BASE_URL}/logout`);

    // 4) 再ログイン: password → totp_required → コード → 成功
    await login(page, totpNow(secret));
    expect(page.url()).not.toContain("/login");

    // 5) TOTP コード無し (空 submit のまま放置) ではダッシュボードに入れないことの確認:
    //    ログアウト後、password だけ入れて submit すると totp-section が出る (ログイン未完了)。
    await page.goto(`${BASE_URL}/logout`);
    await page.goto(`${BASE_URL}/login`);
    await page.fill('input[name="username"]', USERNAME);
    await page.fill('input[name="password"]', PASSWORD);
    await page.click('input[type="submit"]');
    await expect(page.locator("#totp-section")).toBeVisible({ timeout: 30000 });
    expect(page.url()).toContain("/login");
  });
});
