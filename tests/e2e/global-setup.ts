import { execSync, spawn, ChildProcess } from "child_process";
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
from app.services.ai_receipt import encrypt_api_key

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

    # AI 設定（Ollama → mock サーバー localhost:11435）
    config = UserAIConfig.query.filter_by(user_id=u.id).first()
    if not config:
        config = UserAIConfig(
            user_id=u.id,
            provider='ollama',
            model_name='mock',
            base_url='http://localhost:11435',
            api_key_encrypted=encrypt_api_key('_'),
        )
        db.session.add(config)
    else:
        config.provider = 'ollama'
        config.model_name = 'mock'
        config.base_url = 'http://localhost:11435'
        config.api_key_encrypted = encrypt_api_key('_')
    db.session.commit()

    # アカウント ID 取得
    cash = Account.query.filter_by(user_id=u.id, code='1010').first()
    food = Account.query.filter_by(user_id=u.id, code='5010').first()
    ids = {'cash': cash.id, 'food': food.id}

    # 月次確定データ作成（tojson バグ再現用）
    from app.models.fiscal import FiscalClose
    fc = FiscalClose.query.filter_by(user_id=u.id, year=2025).first()
    if not fc:
        fc = FiscalClose(user_id=u.id, year=2025, closed_period=12)
        db.session.add(fc)
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
            {'account_id': food.id, 'account_name': '食費',
             'debit_amount': 1500, 'credit_amount': 0},
            {'account_id': cash.id, 'account_name': '現金',
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
    print('ACCOUNT_IDS=' + json.dumps(ids))
`;

/**
 * E2Eテスト用ユーザーをDBに作成し、モック AI サーバーをコンテナ内で起動する。
 */
export default function globalSetup() {
  const escaped = SETUP_SCRIPT.replace(/"/g, '\\"');
  const execCmd = process.env.CI
    ? `python -c "${escaped}"`
    : `docker compose exec -T web python -c "${escaped}"`;

  const result = execSync(execCmd, { encoding: "utf-8", timeout: 30000 });
  console.log("global-setup:", result.trim());

  // ACCOUNT_IDS を抽出
  const match = result.match(/ACCOUNT_IDS=(\{.*\})/);
  const ids = match ? JSON.parse(match[1]) : { cash: 1, food: 2 };

  // モック AI サーバーをコンテナ内で起動（バックグラウンド）
  const mockScript = path.resolve(__dirname, "mock-ai-server.py");
  const mockCmd = process.env.CI
    ? `nohup python ${mockScript} --cash-id ${ids.cash} --food-id ${ids.food} > /tmp/mock-ai.log 2>&1 &`
    : `docker compose exec -T -d web python /app/tests/e2e/mock-ai-server.py --cash-id ${ids.cash} --food-id ${ids.food}`;

  execSync(mockCmd, { encoding: "utf-8", timeout: 10000 });

  // サーバー起動を待つ
  const checkScript = `import urllib.request, json; r = urllib.request.urlopen(urllib.request.Request('http://localhost:11435/v1/chat/completions', data=json.dumps({'messages':[]}).encode(), headers={'Content-Type':'application/json'})); print('OK', r.status)`;
  const checkCmd = process.env.CI
    ? `python -c "${checkScript}"`
    : `docker compose exec -T web python -c "${checkScript}"`;

  for (let i = 0; i < 5; i++) {
    try {
      const check = execSync(checkCmd, { encoding: "utf-8", timeout: 5000 });
      if (check.includes("OK")) {
        console.log("global-setup: mock AI server started");
        return;
      }
    } catch {
      execSync("sleep 1");
    }
  }
  console.warn("global-setup: mock AI server may not have started");
}
