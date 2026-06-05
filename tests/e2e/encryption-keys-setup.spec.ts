// 暗号鍵管理ウィザードの「実際の鍵生成」E2E (E1 / 回帰)。
//
// 既存 encryption-keys.spec.ts はUI表示・遷移のみを検証していたため、ウィザードの
// 鍵生成フロー (SharedCryptoClient.generateKey/wrap → createWrappedKey) は一度も
// ブラウザで実行されておらず、以下のリグレッションを見逃していた:
//   - this._client を Alpine の reactive プロパティに載せたため private メソッド
//     (#send) アクセスが全エンジンで失敗 ("object is not the right class" /
//     "Cannot access private method (this.#send)")。鍵設定が全方式で不能。
//   - kdf_params のフィールド名不一致 (client: memorySize / server: memory) で
//     createWrappedKey が HTTP 400。
//
// 本テストはパスフレーズ方式で鍵生成 → 解錠まで通す。Argon2id (hash-wasm) は
// vendored (/static/js/vendor) なので CDN 非依存・CI 安全。Passkey 方式は実機
// authenticator が要るため E2E 不能だが、同じ this._client 経路を通るため
// パスフレーズ E2E が proxy リグレッションの回帰ガードになる。

import { test, expect } from "@playwright/test";
import { spawnSync } from "child_process";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_keysetup";
const PASSWORD = "e2e_pass_12345"; // gitleaks:allow E2E テスト用ダミー
const PASSPHRASE = "correct horse battery staple 12345";

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
        delete_user_account(ex.id)  # 鍵を毎回まっさらに (初回設定の前提)
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
    page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 15000 }),
    page.click('input[type="submit"], button[type="submit"]'),
  ]);
}

test.describe("暗号鍵ウィザード: パスフレーズ方式の鍵生成 (回帰)", () => {
  test.beforeEach(() => {
    const out = runPython(SEED_SCRIPT);
    if (!/KEYSETUP_UID=\d+/.test(out)) throw new Error("seed failed: " + out);
  });

  test("初回設定 → パスフレーズで鍵生成 → done、再読込で解錠できる", async ({ page }) => {
    test.setTimeout(60000);
    await login(page);
    await page.goto(`${BASE_URL}/settings/encryption-keys`, { waitUntil: "networkidle" });

    // 初回設定 → パスフレーズ
    await page.click('button:has-text("初回設定を開始")');
    await page.click('button:has-text("パスフレーズ")');
    const pw = page.locator('input[type="password"][autocomplete="new-password"]');
    await pw.nth(0).fill(PASSPHRASE);
    await pw.nth(1).fill(PASSPHRASE);
    await page.click('button:has-text("登録する")');

    // 完了画面 (step=done) に到達 = generateKey/wrap/createWrappedKey が成功
    await expect(page.locator("text=鍵を登録しました")).toBeVisible({ timeout: 30000 });
    // proxy / kdf_params のリグレッションなら error が x-show で可視化される
    // (.alert-danger は x-show で hidden でも DOM 上は存在するので :visible で判定)
    await expect(page.locator(".alert-danger:visible")).toHaveCount(0);

    // 再読込 → 登録済み鍵が一覧表示され、その鍵で解錠できる
    await page.reload({ waitUntil: "networkidle" });
    await page.click('button:has-text("この鍵で解除")');
    await page.fill('input[type="password"][autocomplete="current-password"]', PASSPHRASE);
    await page.click('button:has-text("解除する")');
    // 解錠成功で start 画面に戻り「解除済み」バッジが出る
    await expect(page.locator("text=解除済み")).toBeVisible({ timeout: 30000 });
  });
});
