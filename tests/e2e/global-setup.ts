import { execSync } from "child_process";

/**
 * E2Eテスト用ユーザーをDBに作成する。
 * 既に存在すればパスワードをリセットする。
 */
export default function globalSetup() {
  const script = `
from app import create_app
from app.extensions import db
from app.models.user import User

app = create_app()
with app.app_context():
    u = User.query.filter_by(username='e2e_test').first()
    if not u:
        u = User(username='e2e_test', email='e2e@test.local', user_type='personal')
        db.session.add(u)
    u.set_password('e2e_pass_12345')
    db.session.commit()
    print('OK user_id=', u.id)
`;
  const result = execSync(
    `docker compose exec -T web python -c "${script.replace(/"/g, '\\"')}"`,
    { encoding: "utf-8", timeout: 15000 }
  );
  console.log("global-setup:", result.trim());
}
