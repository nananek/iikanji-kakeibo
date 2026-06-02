"""E5 #112 PR-B: 監査スナップショット送信ページ (GET /settings/audit/<id>/packages)。

owner 側の送信 UI を描画するビューのテスト。HPKE seal / 送信自体はクライアント
(packages_renderer.mjs) が行うため、ここでは認可・科目メタ・年度・grant 状態の
描画を検証する。
"""

import json
import re

from app.models.audit import AuditGrant, AuditGrantAccount


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _grant(db, owner, auditor, level=1, revoked=False):
    from datetime import datetime, timezone

    g = AuditGrant(
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=level,
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    )
    db.session.add(g)
    db.session.commit()
    return g


def _config(html):
    m = re.search(
        r'id="audit-packages-config"[^>]*>(.*?)</script>', html, re.S,
    )
    assert m is not None, "config JSON island not found"
    return json.loads(m.group(1))


def test_packages_page_requires_login(client, db):
    resp = client.get("/settings/audit/1/packages")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_packages_page_renders_for_owner(client, db, user, auditor, accounts):
    grant = _grant(db, user, auditor, level=1)
    _login(client, user)
    resp = client.get(f"/settings/audit/{grant.id}/packages")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "auditor" in html  # 監査者名
    cfg = _config(html)
    assert cfg["grant_id"] == grant.id
    assert cfg["auditor_id"] == auditor.id
    assert cfg["permission_level"] == 1
    # Lv1 は全科目をメタに含む
    assert "5010" in cfg["accounts_meta"]
    assert "5020" in cfg["accounts_meta"]
    assert cfg["accounts_meta"]["5010"]["name"] == "食費"


def test_packages_page_renders_responses_review(client, db, user, auditor, accounts):
    """監査者からの修正案レビュー UI (受信カード + renderer 結線) が描画される"""
    grant = _grant(db, user, auditor, level=1)
    _login(client, user)
    resp = client.get(f"/settings/audit/{grant.id}/packages")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "audit-responses-review" in html
    assert "responses_review_renderer.mjs" in html
    assert "initResponsesReview" in html


def test_packages_page_404_for_non_owner_grant(client, db, user, auditor, accounts):
    """他人が owner の grant は 404 (IDOR)。"""
    from app.models.user import User

    other = User(username="other", email="other@example.com", user_type="personal")
    other.set_password("pw")
    db.session.add(other)
    db.session.commit()
    grant = _grant(db, other, auditor, level=1)

    _login(client, user)
    resp = client.get(f"/settings/audit/{grant.id}/packages")
    assert resp.status_code == 404


def test_packages_page_revoked_redirects(client, db, user, auditor, accounts):
    grant = _grant(db, user, auditor, level=1, revoked=True)
    _login(client, user)
    resp = client.get(f"/settings/audit/{grant.id}/packages")
    assert resp.status_code == 302
    assert "/settings/audit" in resp.headers["Location"]


def test_packages_page_auditor_user_redirected(client, db, auditor):
    """監査ユーザー (user_type=auditor) は個人専用ページから追い出される。"""
    _login(client, auditor)
    # auditor が owner の grant は存在しない。個人専用ガードが先に効く。
    resp = client.get("/settings/audit/1/packages")
    assert resp.status_code == 302


def test_lv2_packages_meta_only_published(client, db, user, auditor, accounts):
    """Lv2 は公開科目のみメタに含め、非公開科目名を漏らさない。"""
    grant = _grant(db, user, auditor, level=2)
    db.session.add(AuditGrantAccount(
        audit_grant_id=grant.id, account_user_id=user.id, account_code="5010",
    ))
    db.session.commit()

    _login(client, user)
    resp = client.get(f"/settings/audit/{grant.id}/packages")
    assert resp.status_code == 200
    cfg = _config(resp.data.decode())
    assert "5010" in cfg["accounts_meta"]      # 公開
    assert "5020" not in cfg["accounts_meta"]  # 非公開は漏れない
