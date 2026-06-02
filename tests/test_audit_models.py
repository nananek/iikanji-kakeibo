"""E5 #112 PR-B: audit_packages / audit_responses モデルのテスト。

スキーマ制約 (UNIQUE / CHECK / CASCADE)、revoked_at 既定、expires_at の
90 日 TTL 既定、リレーション往復を検証する。
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import User
from app.models.audit import (
    AUDIT_PACKAGE_TTL,
    AuditGrant,
    AuditPackage,
    AuditResponse,
)


def _user(db, name=None):
    name = name or f"u{uuid4().hex[:8]}"
    u = User(username=name, email=f"{name}@e.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _grant(db, owner, auditor, level=2):
    g = AuditGrant(
        owner_user_id=owner.id,
        auditor_user_id=auditor.id,
        permission_level=level,
    )
    db.session.add(g)
    db.session.commit()
    return g


def _package(db, grant, owner, auditor, round_id=1, level=2):
    pkg = AuditPackage(
        audit_grant_id=grant.id,
        round_id=round_id,
        owner_user_id=owner.id,
        auditor_user_id=auditor.id,
        permission_level=level,
        ephemeral_pubkey=b"\x01" * 32,
        ciphertext=b"\x02" * 50,
        snapshot_hash=b"\x03" * 32,
    )
    db.session.add(pkg)
    db.session.commit()
    return pkg


# --- AuditGrant.revoked_at ----------------------------------------------


def test_grant_revoked_at_defaults_null(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    assert g.revoked_at is None


def test_grant_revoked_at_settable(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    g.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    assert db.session.get(AuditGrant, g.id).revoked_at is not None


# --- AuditPackage --------------------------------------------------------


def test_package_roundtrip_and_expires_default(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _package(db, g, owner, auditor)

    fetched = db.session.get(AuditPackage, pkg.id)
    assert fetched.ephemeral_pubkey == b"\x01" * 32
    assert fetched.owner_accepted_at is None
    # expires_at は created_at + 90 日 (おおよそ)
    delta = fetched.expires_at - fetched.created_at
    assert abs(delta - AUDIT_PACKAGE_TTL).total_seconds() < 5


def test_package_permission_level_check(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    bad = AuditPackage(
        audit_grant_id=g.id, round_id=1,
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=4,  # 1/2/3 以外は CHECK 違反
        ephemeral_pubkey=b"\x01" * 32,
        ciphertext=b"\x02" * 10, snapshot_hash=b"\x03" * 32,
    )
    db.session.add(bad)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_package_owner_accepted_at_roundtrip(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _package(db, g, owner, auditor)
    now = datetime.now(timezone.utc)
    pkg.owner_accepted_at = now
    db.session.commit()
    assert db.session.get(AuditPackage, pkg.id).owner_accepted_at is not None


def test_package_unique_grant_round(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _package(db, g, owner, auditor, round_id=1)
    # 同じ (grant, round) は UNIQUE 違反
    dup = AuditPackage(
        audit_grant_id=g.id, round_id=1,
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=2, ephemeral_pubkey=b"\x01" * 32,
        ciphertext=b"\x02" * 10, snapshot_hash=b"\x03" * 32,
    )
    db.session.add(dup)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_package_different_rounds_ok(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _package(db, g, owner, auditor, round_id=1)
    _package(db, g, owner, auditor, round_id=2)
    assert AuditPackage.query.filter_by(audit_grant_id=g.id).count() == 2


def test_deleting_grant_cascades_packages(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _package(db, g, owner, auditor)
    pkg_id = pkg.id
    db.session.delete(g)
    db.session.commit()
    assert db.session.get(AuditPackage, pkg_id) is None


# --- AuditResponse -------------------------------------------------------


def _response(db, pkg, response_type="revision"):
    r = AuditResponse(
        audit_package_id=pkg.id,
        response_type=response_type,
        ephemeral_pubkey=b"\x04" * 32,
        ciphertext=b"\x05" * 40,
    )
    db.session.add(r)
    db.session.commit()
    return r


def test_response_roundtrip(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _package(db, g, owner, auditor)
    r = _response(db, pkg, "rejection")
    fetched = db.session.get(AuditResponse, r.id)
    assert fetched.response_type == "rejection"
    assert fetched.owner_acknowledged_at is None
    assert pkg.responses[0].id == r.id
    # owner_acknowledged_at をセットして永続化・再読込
    fetched.owner_acknowledged_at = datetime.now(timezone.utc)
    db.session.commit()
    assert db.session.get(AuditResponse, r.id).owner_acknowledged_at is not None


def test_response_type_validation_rejects_bad_value(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _package(db, g, owner, auditor)
    # @validates がアプリ層で弾く (DB 到達前に ValueError)
    with pytest.raises(ValueError):
        AuditResponse(
            audit_package_id=pkg.id,
            response_type="bogus",
            ephemeral_pubkey=b"\x04" * 32,
            ciphertext=b"\x05" * 10,
        )


def test_deleting_package_cascades_responses(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _package(db, g, owner, auditor)
    r = _response(db, pkg)
    r_id = r.id
    db.session.delete(pkg)
    db.session.commit()
    assert db.session.get(AuditResponse, r_id) is None
