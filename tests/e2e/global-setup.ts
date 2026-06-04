import { execSync, execFileSync, spawnSync, spawn, ChildProcess } from "child_process";
import * as path from "path";
import * as fs from "fs";

const SETUP_SCRIPT = `
import json
from app import create_app
from app.extensions import db
from app.models.user import User
from app.models.account import Account
from app.models.ai_draft import AIDraft
from app.models.ai_config import UserAIConfig
from app.services.seed import seed_accounts_for_user

app = create_app()
with app.app_context():
    # ユーザー作成
    u = User.query.filter_by(username='e2e_test').first()
    if not u:
        u = User(username='e2e_test', email='e2e@test.local', user_type='personal')
        u.set_password('e2e_pass_12345')
        db.session.add(u)
        db.session.flush()
    else:
        u.set_password('e2e_pass_12345')
    db.session.commit()

    # 標準科目シード
    seed_accounts_for_user(u.id)

    # AI 設定: E2EE 形式 (api_key_blob/iv は dummy)。E2EE 化以降サーバ側
    # LLM 呼出は無いので Web 上で is_e2ee=True と判定されれば十分。
    config = UserAIConfig.query.filter_by(user_id=u.id).first()
    if not config:
        config = UserAIConfig(
            user_id=u.id,
            provider='openai',
            model_name='gpt-4o',
            api_key_blob=b'\\xAA' * 48,
            api_key_iv=b'\\xBB' * 12,
        )
        db.session.add(config)
    else:
        config.provider = 'openai'
        config.model_name = 'gpt-4o'
        config.api_key_blob = b'\\xAA' * 48
        config.api_key_iv = b'\\xBB' * 12
    db.session.commit()

    # アカウントコード取得
    cash = Account.query.filter_by(user_id=u.id, code='1010').first()
    food = Account.query.filter_by(user_id=u.id, code='5010').first()
    codes = {'cash': cash.code, 'food': food.code}

    # 月次確定データ作成（tojson バグ再現用）
    from app.models.fiscal import FiscalClose
    fc = FiscalClose.query.filter_by(user_id=u.id, year=2025).first()
    if not fc:
        fc = FiscalClose(user_id=u.id, year=2025, closed_period=12)
        db.session.add(fc)
        db.session.commit()

    # テスト用仕訳を作成（残高試算表E2Eテスト用）
    from app.models.journal import JournalEntry, JournalEntryLine
    if not JournalEntry.query.filter_by(user_id=u.id).first():
        from app.services.accounting import get_next_entry_number
        # E3-F PR-D-6-5 (055): 平文 date/description/source 列は DROP 済。
        # fiscal_year/fiscal_month のみ populate する (本番 create_journal_entry 同様)。
        entry = JournalEntry(
            user_id=u.id,
            entry_number=get_next_entry_number(u.id),
            fiscal_year=2026, fiscal_month=1,
        )
        # #338 item8 (068): 平文 account_code/debit/credit 列は DROP 済。本体は
        # encrypted_blob のみ (E2E seed ではダミー blob)。
        entry.lines = [
            JournalEntryLine(account_user_id=u.id,
                             encrypted_blob=bytes([0x42]) * 48,
                             blob_iv=bytes([0x42]) * 12),
            JournalEntryLine(account_user_id=u.id,
                             encrypted_blob=bytes([0x42]) * 48,
                             blob_iv=bytes([0x42]) * 12),
        ]
        db.session.add(entry)
        db.session.commit()

    # テスト用 analyzed ドラフト作成（既存があれば削除して再作成）
    AIDraft.query.filter_by(user_id=u.id, status='analyzed').delete()
    db.session.commit()
    suggestions = [{
        'title': '食費として計上',
        'description': 'テスト用ドラフト',
        'date': '2026-01-15',
        'entry_description': 'テスト商店',
        'lines': [
            {'account_code': food.code, 'account_name': '食費',
             'debit_amount': 1500, 'credit_amount': 0},
            {'account_code': cash.code, 'account_name': '現金',
             'debit_amount': 0, 'credit_amount': 1500},
        ],
    }]
    from app.services.storage import get_storage_backend
    key = f'vouchers/{u.id}/e2e_test.jpg'
    get_storage_backend().put(key, b'\\xff\\xd8\\xff', 'image/jpeg')
    draft = AIDraft(
        user_id=u.id,
        image_key=key,
        image_mime='image/jpeg',
        suggestions_json=json.dumps(suggestions, ensure_ascii=False),
        status='analyzed',
    )
    db.session.add(draft)
    db.session.commit()

    print('OK user_id=' + str(u.id))
    print('ACCOUNT_CODES=' + json.dumps(codes))
`;

/**
 * Python を CI / docker 双方向けに stdin 経由で起動する。
 * shell 補間を介さないため、引数のサニタイズ抜けに依存しない。
 */
function runPython(stdinScript: string, timeoutMs = 30000) {
  const [cmd, ...args] = process.env.CI
    ? ["python", "-"]
    : ["docker", "compose", "exec", "-T", "web", "python", "-"];
  const result = spawnSync(cmd, args, {
    input: stdinScript,
    encoding: "utf-8",
    timeout: timeoutMs,
  });
  if (result.status !== 0) {
    throw new Error(`python invocation failed (status=${result.status}): ${result.stderr}`);
  }
  return result.stdout;
}

/** account_code を 4 桁数字（標準科目コード）に限定する */
function sanitizeAccountCode(c: unknown, fallback: string) {
  return typeof c === "string" && /^\d{4}$/.test(c) ? c : fallback;
}

/**
 * E2Eテスト用ユーザーをDBに作成し、モック AI サーバーをコンテナ内で起動する。
 */
export default function globalSetup() {
  const result = runPython(SETUP_SCRIPT);
  console.log("global-setup:", result.trim());

  // ACCOUNT_CODES を抽出（数字 4 桁のみ許可）
  const match = result.match(/ACCOUNT_CODES=(\{.*\})/);
  const parsed = match ? JSON.parse(match[1]) : {};
  const codes = {
    cash: sanitizeAccountCode(parsed.cash, "1010"),
    food: sanitizeAccountCode(parsed.food, "5010"),
  };

  // モック AI サーバーをコンテナ内で起動（引数は spawn の配列で渡す）
  const mockScript = path.resolve(__dirname, "mock-ai-server.py");
  if (process.env.CI) {
    // CI: nohup でバックグラウンド起動。引数は配列で安全に渡す。
    const child = spawn(
      "python",
      [mockScript, "--cash-code", codes.cash, "--food-code", codes.food],
      {
        detached: true,
        stdio: ["ignore", fs.openSync("/tmp/mock-ai.log", "a"), fs.openSync("/tmp/mock-ai.log", "a")],
      },
    );
    child.unref();
  } else {
    execFileSync(
      "docker",
      [
        "compose", "exec", "-T", "-d", "web",
        "python", "/app/tests/e2e/mock-ai-server.py",
        "--cash-code", codes.cash,
        "--food-code", codes.food,
      ],
      { encoding: "utf-8", timeout: 10000 },
    );
  }

  // サーバー起動を待つ
  const checkScript = `import urllib.request, json
r = urllib.request.urlopen(urllib.request.Request('http://localhost:11435/v1/chat/completions', data=json.dumps({'messages':[]}).encode(), headers={'Content-Type':'application/json'}))
print('OK', r.status)
`;
  for (let i = 0; i < 5; i++) {
    try {
      const check = runPython(checkScript, 5000);
      if (check.includes("OK")) {
        console.log("global-setup: mock AI server started");
        return;
      }
    } catch {
      execFileSync("sleep", ["1"]);
    }
  }
  console.warn("global-setup: mock AI server may not have started");
}
