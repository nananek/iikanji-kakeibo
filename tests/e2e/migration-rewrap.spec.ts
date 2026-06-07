// #385 PR-3b: v4 平文→E2EE 透過移行 (ログイン駆動) のブラウザ E2E。
//
// 「メンテナンスウィンドウで genkey 済 (サーバが temp-MK で全台帳を暗号化した)」
// 状態の v4 ユーザー (werkzeug password_hash・login_salt 未設定) が、**ログイン画面で
// パスワードを入力するだけ**で透過移行を完遂し、データが壊れないことを検証する。
//
// ログイン押下で login_flow.mjs が ① /auth/login/begin ② 実 Argon2id で master 導出
// ③ HKDF split ④ MK 生成 + wrap ⑤ /auth/login/finish 移行 ⑥ 鍵ペア確立 ⑦ temp-MK→自 MK
// rewrap ⑧ finalize までをログインページ上で完遂してから redirect する。検証では
// MK をログインパスワード + login_salt から再導出 (通常ログインと同じ HKDF split 解錠)
// して復号照合する。
//
// finalize が temp_mk を破棄する (片道) ため、beforeEach で移行状態を毎回作り直す
// (retry 安全)。

import { test, expect } from "@playwright/test";
import { runPython } from "./helpers";

const BASE_URL = "http://127.0.0.1:5000";
const USERNAME = "e2e_migrate";
const PASSWORD = "e2e_pass_12345"; // gitleaks:allow E2E テスト用のダミーパスワード (秘密情報ではない)
const YEAR = 2026;

// シード (migration_crypto) が temp-MK で暗号化する原本。透過移行後に自 MK で
// 復号した結果がこれと一致すれば「データ無損傷」。
const EXPECTED = [
  { desc: "テスト食費", lines: [
    { account_code: "5010", debit_amount: 1500, credit_amount: 0 },
    { account_code: "1010", debit_amount: 0, credit_amount: 1500 }] },
  { desc: "テスト住居費", lines: [
    { account_code: "5020", debit_amount: 80000, credit_amount: 0 },
    { account_code: "1020", debit_amount: 0, credit_amount: 80000 }] },
];

// 移行状態ユーザーを seed する Python (global-setup と同じ stdin 方式)。
// migrate-e2ee-data (genkey) が出力するのと同一フォーマットの temp-MK 暗号文を
// 現スキーマ上で直接構築する (平文列は DROP 済のため migrate-e2ee-data 本体は
// 使えない)。
const SEED_SCRIPT = `
from datetime import date
from app import create_app
from app.extensions import db
from app.models.user import User
from app.models.journal import JournalEntry, JournalEntryLine
from app.services.seed import seed_accounts_for_user
from app.services.accounting import get_next_entry_number
from app.services.migration_crypto import build_aad, encrypt_record
from app.services.e2ee_data_migration import je_record, jel_record
from app.services.account_deletion import delete_user_account

app = create_app()
with app.app_context():
    ex = User.query.filter_by(username='e2e_migrate').first()
    if ex:
        delete_user_account(ex.id)
    u = User(username='e2e_migrate', email='e2e_migrate@test.local',
             user_type='personal')
    u.set_password('e2e_pass_12345')
    db.session.add(u)
    db.session.flush()
    uid = u.id
    seed_accounts_for_user(uid)
    temp_mk = bytes(bytearray((i * 7 + 3) % 256 for i in range(32)))
    u.migration_temp_mk = temp_mk
    db.session.flush()
    aad_je = build_aad('je', uid)
    aad_jel = build_aad('jel', uid)
    specs = [
        ('テスト食費', date(2026, 1, 15), 1,
         [('5010', 1500, 0, 'ランチ'), ('1010', 0, 1500, '現金')]),
        ('テスト住居費', date(2026, 2, 10), 2,
         [('5020', 80000, 0, '家賃'), ('1020', 0, 80000, '普通預金')]),
    ]
    for desc, d, fm, lines in specs:
        je = JournalEntry(user_id=uid, entry_number=get_next_entry_number(uid),
                          fiscal_year=2026, is_closing=False, fiscal_month=fm)
        b, iv = encrypt_record(temp_mk, je_record(d, desc, 'journal', fm), aad_je)
        je.encrypted_blob = b
        je.blob_iv = iv
        je.lines = []
        for acct, deb, cred, ld in lines:
            jl = JournalEntryLine(account_user_id=uid)
            b2, iv2 = encrypt_record(temp_mk, jel_record(acct, deb, cred, ld), aad_jel)
            jl.encrypted_blob = b2
            jl.blob_iv = iv2
            je.lines.append(jl)
        db.session.add(je)
    db.session.commit()
    print('MIGRATE_UID=' + str(uid))
`;


function seedMigrationUser(): number {
  const out = runPython(SEED_SCRIPT);
  const m = out.match(/MIGRATE_UID=(\d+)/);
  if (!m) throw new Error("migration seed failed: " + out);
  return Number(m[1]);
}

async function login(page) {
  await page.goto(`${BASE_URL}/login`);
  await page.fill('input[name="username"]', USERNAME);
  await page.fill('input[name="password"]', PASSWORD);
  // ログイン押下で透過移行 (Argon2id + rewrap + finalize) まで自動駆動されるため長め。
  await Promise.all([
    page.waitForURL((u) => !u.pathname.includes("/login"), { timeout: 30000 }),
    page.click('input[type="submit"], button[type="submit"]'),
  ]);
}

test.describe("#385 v4 透過移行 (ログイン駆動)", () => {
  let uid: number;

  test.beforeEach(() => {
    uid = seedMigrationUser();
  });

  test("ログインだけで透過移行が完遂しデータ無損傷", async ({ page }) => {
    test.setTimeout(120000);
    // ログイン押下で login_flow が透過移行 (Argon2id → MK 生成 → wrap → 鍵ペア →
    // temp-MK→自 MK rewrap → finalize) をログインページ上で完遂してから redirect する。
    await login(page);
    expect(page.url()).not.toContain("/login");

    // 透過移行は finalize まで完了 → ダッシュボードに再ラップバナーは出ない。
    await expect(page.locator("#migration-rewrap-btn")).toHaveCount(0);

    // 検証: MK をログインパスワード + login_salt から再導出 (= 通常ログインと同じ
    // HKDF split 解錠) し、rewrap 後データを復号して原本と照合する。Playwright は
    // 遷移で SharedWorker を破棄するため (実ブラウザは保持)、ここで MK を張り直す。
    const v = await page.evaluate(async ({ uid, year, password }) => {
      try {
        const loader = await import("/static/js/crypto/hash_wasm_loader.js");
        const kdf = await import("/static/js/crypto/login_kdf.js");
        const api = await import("/static/js/crypto/api.js");
        const sc = await import("/static/js/crypto/shared-client.js");
        const rec = await import("/static/js/crypto/record.js");
        const b64 = await import("/static/js/crypto/b64.js");
        await loader.loadHashWasm();
        const keys = await api.listWrappedKeys();
        const pp = keys.find((k) => k.method === "passphrase");
        if (!pp) return { error: "passphrase wrapped_key not found" };
        const { mkWrapKey } = await kdf.deriveLoginMaterial(
          password, pp.salt, { params: pp.kdf_params });
        const client = new sc.SharedCryptoClient("/static/js/crypto/shared-worker.js");
        await client.unwrap(mkWrapKey, pp.wrapped_master_key, pp.wrap_iv);

        const body = await (await fetch(
          `/api/v1/journals?fiscal_year=${year}&per_page=100`,
          { credentials: "include" })).json();
        const out = [];
        for (const e of body.journals || []) {
          const je = await rec.decryptRecord(client,
            b64.b64decode(e.encrypted_blob), b64.b64decode(e.blob_iv),
            rec.buildAAD("je", uid));
          const lines = [];
          for (const l of e.lines || []) {
            lines.push(await rec.decryptRecord(client,
              b64.b64decode(l.encrypted_blob), b64.b64decode(l.blob_iv),
              rec.buildAAD("jel", uid)));
          }
          out.push({ je, lines });
        }
        const tm = await (await fetch("/api/v1/migration/temp-mk",
          { credentials: "include" })).json();
        return { decrypted: out, tempmk: tm };
      } catch (e) {
        return { error: String((e && e.message) || e) };
      }
    }, { uid, year: YEAR, password: PASSWORD });

    expect(v.error || "").toBe("");
    expect(v.decrypted.length).toBe(EXPECTED.length);
    for (const exp of EXPECTED) {
      const got = v.decrypted.find((d) => d.je && d.je.description === exp.desc);
      expect(got, `entry "${exp.desc}" not found`).toBeTruthy();
      for (const el of exp.lines) {
        const gl = got.lines.find((l) => l.account_code === el.account_code);
        expect(gl, `line ${el.account_code} missing`).toBeTruthy();
        expect(gl.debit_amount).toBe(el.debit_amount);
        expect(gl.credit_amount).toBe(el.credit_amount);
      }
    }
    // finalize 済 → temp-MK はもう取得できない (真の E2EE 確立)。
    expect(v.tempmk.active).toBe(false);

    // 仕訳帳一覧ページが 500 にならない (UI 健全性)。
    const resp = await page.goto(`${BASE_URL}/journal/`, { waitUntil: "domcontentloaded" });
    expect(resp?.status()).toBe(200);
  });
});
