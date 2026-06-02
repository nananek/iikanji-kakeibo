"""E5 #112 PR-J: flask audit-cleanup CLI のテスト。

設計書 §14.8: 期限切れ (expires_at 経過) の AuditPackage を 90 日 TTL で自動削除し、
紐づく AuditResponse を CASCADE 削除する。
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models import User
from app.models.audit import AuditGrant, AuditPackage, AuditResponse


def _user(db, name=None):
    name = name or f"u{uuid4().hex[:8]}"
    u = User(username=name, email=f"{name}@e.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _grant(db, owner, auditor, level=2):
    g = AuditGrant(
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=level,
    )
    db.session.add(g)
    db.session.commit()
    return g


def _package(db, grant, owner, auditor, round_id, expires_at):
    pkg = AuditPackage(
        audit_grant_id=grant.id, round_id=round_id,
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=2,
        ephemeral_pubkey=b"\x01" * 32, ciphertext=b"\x02" * 20,
        snapshot_hash=b"\x03" * 32, expires_at=expires_at,
    )
    db.session.add(pkg)
    db.session.commit()
    return pkg


def _setup(db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    now = datetime.now(timezone.utc)
    expired = _package(db, g, owner, auditor, 1, now - timedelta(days=1))
    fresh = _package(db, g, owner, auditor, 2, now + timedelta(days=30))
    # 期限切れ package に response を 1 件ぶら下げる (CASCADE 削除の確認用)
    resp = AuditResponse(
        audit_package_id=expired.id, response_type="revision",
        ephemeral_pubkey=b"\x04" * 32, ciphertext=b"\x05" * 10,
    )
    db.session.add(resp)
    db.session.commit()
    return expired, fresh, resp


def test_audit_cleanup_deletes_expired_and_cascades(db, app):
    expired, fresh, resp = _setup(db)
    expired_id, fresh_id, resp_id = expired.id, fresh.id, resp.id

    result = app.test_cli_runner().invoke(args=["audit-cleanup"])
    assert result.exit_code == 0
    assert "deleted AuditPackage=1" in result.output

    db.session.expire_all()
    # 期限切れは削除、有効は残る
    assert db.session.get(AuditPackage, expired_id) is None
    assert db.session.get(AuditPackage, fresh_id) is not None
    # response は CASCADE で削除
    assert db.session.get(AuditResponse, resp_id) is None


def test_audit_cleanup_dry_run_deletes_nothing(db, app):
    expired, fresh, resp = _setup(db)
    expired_id = expired.id

    result = app.test_cli_runner().invoke(args=["audit-cleanup", "--dry-run"])
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "1 件" in result.output

    db.session.expire_all()
    # 何も削除されていない
    assert db.session.get(AuditPackage, expired_id) is not None
    assert AuditPackage.query.count() == 2


def test_audit_cleanup_no_expired(db, app):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _package(db, g, owner, auditor, 1,
             datetime.now(timezone.utc) + timedelta(days=10))

    result = app.test_cli_runner().invoke(args=["audit-cleanup"])
    assert result.exit_code == 0
    assert "deleted AuditPackage=0" in result.output
    assert AuditPackage.query.count() == 1
