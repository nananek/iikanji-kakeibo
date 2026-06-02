"""E5 #112 PR-C: 監査連携 API (audit-packages / audit-responses + 公開鍵取得) のテスト。

設計書 §14.5 / §14.11。IDOR / 失効 / round / 期限 / 代理閲覧遮断を重点検証する。
"""

from base64 import b64encode
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.extensions import limiter
from app.models import User
from app.models.audit import AuditGrant, AuditPackage, AuditResponse


@pytest.fixture(autouse=True)
def _reset_rate_limits(app):
    with app.app_context():
        try:
            limiter.reset()
        except Exception:
            pass
    yield


def _user(db, name=None):
    name = name or f"u{uuid4().hex[:8]}"
    u = User(username=name, email=f"{name}@e.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _grant(db, owner, auditor, level=2, revoked=False):
    g = AuditGrant(
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=level, status="draft",
    )
    if revoked:
        g.revoked_at = datetime.now(timezone.utc)
    db.session.add(g)
    db.session.commit()
    return g


def _b(x):
    return b64encode(x).decode()


def _pkg_payload(grant, level=2, round_id=1, eph=b"\x01" * 32,
                 ct=b"\x02" * 20, sh=b"\x03" * 32):
    return {
        "audit_grant_id": grant.id, "round_id": round_id,
        "permission_level": level,
        "ephemeral_pubkey": _b(eph), "ciphertext": _b(ct), "snapshot_hash": _b(sh),
    }


def _make_package(db, grant, owner, auditor, round_id=1, level=2, expires_at=None):
    pkg = AuditPackage(
        audit_grant_id=grant.id, round_id=round_id,
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=level,
        ephemeral_pubkey=b"\x01" * 32, ciphertext=b"\x02" * 20,
        snapshot_hash=b"\x03" * 32,
    )
    if expires_at is not None:
        pkg.expires_at = expires_at
    db.session.add(pkg)
    db.session.commit()
    return pkg


# === POST /audit-packages ===============================================


def test_owner_creates_package(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor, level=2)
    _login(client, owner)
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g))
    assert r.status_code == 201
    data = r.get_json()
    # owner/auditor はサーバが grant から導出 (クライアント値を信用しない)
    assert data["owner_user_id"] == owner.id
    assert data["auditor_user_id"] == auditor.id
    assert data["permission_level"] == 2
    assert data["owner_accepted_at"] is None


def test_non_owner_cannot_create_package(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _login(client, auditor)  # auditor は作成不可
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g))
    assert r.status_code == 403


def test_stranger_cannot_create_package(client, db):
    owner, auditor, stranger = _user(db), _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _login(client, stranger)
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g))
    assert r.status_code == 403


def test_create_on_revoked_grant_403(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor, revoked=True)
    _login(client, owner)
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g))
    assert r.status_code == 403


def test_create_duplicate_grant_round_409(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _login(client, owner)
    assert client.post("/api/v1/audit-packages", json=_pkg_payload(g, round_id=1)).status_code == 201
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g, round_id=1))
    assert r.status_code == 409


def test_create_permission_mismatch_400(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor, level=2)
    _login(client, owner)
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g, level=3))
    assert r.status_code == 400


def test_create_bad_pubkey_length_400(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _login(client, owner)
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g, eph=b"\x01" * 31))
    assert r.status_code == 400


def test_create_bad_hash_length_400(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _login(client, owner)
    r = client.post("/api/v1/audit-packages", json=_pkg_payload(g, sh=b"\x03" * 31))
    assert r.status_code == 400


def test_create_unknown_grant_404(client, db):
    owner = _user(db)
    _login(client, owner)
    r = client.post("/api/v1/audit-packages", json={
        "audit_grant_id": 99999, "round_id": 1, "permission_level": 2,
        "ephemeral_pubkey": _b(b"\x01" * 32), "ciphertext": _b(b"\x02" * 10),
        "snapshot_hash": _b(b"\x03" * 32),
    })
    assert r.status_code == 404


# === GET /audit-packages ================================================


# 注: Flask-Login は test client のリクエストが既存 app context (db fixture)
# を再利用して current_user を g にキャッシュするため、1 テスト内で複数ユーザーを
# ログインさせると全リクエストが最初のユーザーに解決される。よってロール分離は
# 「1 テスト 1 ログイン」で書く (既存コードの作法)。


def test_owner_sees_own_package(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _make_package(db, g, owner, auditor)
    _login(client, owner)
    assert len(client.get("/api/v1/audit-packages").get_json()["audit_packages"]) == 1


def test_auditor_sees_package(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _make_package(db, g, owner, auditor)
    _login(client, auditor)
    assert len(client.get("/api/v1/audit-packages").get_json()["audit_packages"]) == 1


def test_stranger_sees_no_packages(client, db):
    owner, auditor, stranger = _user(db), _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _make_package(db, g, owner, auditor)
    _login(client, stranger)
    assert client.get("/api/v1/audit-packages").get_json()["audit_packages"] == []


def test_list_packages_role_filter(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    _make_package(db, g, owner, auditor)
    _login(client, owner)
    assert len(client.get("/api/v1/audit-packages?role=owner").get_json()["audit_packages"]) == 1
    assert client.get("/api/v1/audit-packages?role=auditor").get_json()["audit_packages"] == []


# === accept / delete ====================================================


def test_accept_by_owner(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    _login(client, owner)
    r = client.post(f"/api/v1/audit-packages/{pkg.id}/accept")
    assert r.status_code == 200
    assert db.session.get(AuditPackage, pkg.id).owner_accepted_at is not None


def test_accept_by_auditor_forbidden(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    _login(client, auditor)
    assert client.post(f"/api/v1/audit-packages/{pkg.id}/accept").status_code == 403


def test_delete_by_auditor_cascades(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    db.session.add(AuditResponse(
        audit_package_id=pkg.id, response_type="revision",
        ephemeral_pubkey=b"\x04" * 32, ciphertext=b"\x05" * 10,
    ))
    db.session.commit()
    pkg_id = pkg.id
    _login(client, auditor)
    assert client.delete(f"/api/v1/audit-packages/{pkg_id}").status_code == 204
    assert db.session.get(AuditPackage, pkg_id) is None
    assert AuditResponse.query.filter_by(audit_package_id=pkg_id).count() == 0


def test_delete_by_stranger_forbidden(client, db):
    owner, auditor, stranger = _user(db), _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    _login(client, stranger)
    assert client.delete(f"/api/v1/audit-packages/{pkg.id}").status_code == 403


# === audit-responses ====================================================


def _resp_payload(pkg, rtype="revision", eph=b"\x04" * 32, ct=b"\x05" * 20):
    return {
        "audit_package_id": pkg.id, "response_type": rtype,
        "ephemeral_pubkey": _b(eph), "ciphertext": _b(ct),
    }


def test_auditor_creates_response(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    _login(client, auditor)
    r = client.post("/api/v1/audit-responses", json=_resp_payload(pkg))
    assert r.status_code == 201
    assert r.get_json()["response_type"] == "revision"


def test_owner_cannot_create_response(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    _login(client, owner)  # owner は response を返せない
    assert client.post("/api/v1/audit-responses", json=_resp_payload(pkg)).status_code == 403


def test_response_bad_type_400(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    _login(client, auditor)
    assert client.post("/api/v1/audit-responses", json=_resp_payload(pkg, rtype="bogus")).status_code == 400


def test_response_on_expired_package_403(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    pkg = _make_package(db, g, owner, auditor, expires_at=past)
    _login(client, auditor)
    assert client.post("/api/v1/audit-responses", json=_resp_payload(pkg)).status_code == 403


def test_response_on_revoked_grant_403(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    g.revoked_at = datetime.now(timezone.utc)
    db.session.commit()
    _login(client, auditor)
    assert client.post("/api/v1/audit-responses", json=_resp_payload(pkg)).status_code == 403


def _seed_response(db, owner, auditor):
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    db.session.add(AuditResponse(
        audit_package_id=pkg.id, response_type="revision",
        ephemeral_pubkey=b"\x04" * 32, ciphertext=b"\x05" * 10,
    ))
    db.session.commit()


def test_owner_sees_response(client, db):
    owner, auditor = _user(db), _user(db)
    _seed_response(db, owner, auditor)
    _login(client, owner)
    assert len(client.get("/api/v1/audit-responses").get_json()["audit_responses"]) == 1


def test_stranger_sees_no_responses(client, db):
    owner, auditor, stranger = _user(db), _user(db), _user(db)
    _seed_response(db, owner, auditor)
    _login(client, stranger)
    assert client.get("/api/v1/audit-responses").get_json()["audit_responses"] == []


def test_acknowledge_by_owner(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    resp = AuditResponse(
        audit_package_id=pkg.id, response_type="revision",
        ephemeral_pubkey=b"\x04" * 32, ciphertext=b"\x05" * 10,
    )
    db.session.add(resp)
    db.session.commit()
    _login(client, owner)
    r = client.post(f"/api/v1/audit-responses/{resp.id}/acknowledge")
    assert r.status_code == 200
    assert db.session.get(AuditResponse, resp.id).owner_acknowledged_at is not None


def test_acknowledge_by_auditor_forbidden(client, db):
    owner, auditor = _user(db), _user(db)
    g = _grant(db, owner, auditor)
    pkg = _make_package(db, g, owner, auditor)
    resp = AuditResponse(
        audit_package_id=pkg.id, response_type="revision",
        ephemeral_pubkey=b"\x04" * 32, ciphertext=b"\x05" * 10,
    )
    db.session.add(resp)
    db.session.commit()
    _login(client, auditor)
    assert client.post(f"/api/v1/audit-responses/{resp.id}/acknowledge").status_code == 403


# 旧監査代理閲覧 (acting-as) 中の audit-packages/responses 遮断テストは、
# リアルタイム代理閲覧の撤去 (#112) に伴い削除した。これらの API は常にログイン
# 本人 (owner / auditor) の self-service で、IDOR フィルタ (owner_user_id /
# auditor_user_id) により他者のパッケージには到達できない (上記 IDOR テストで担保)。
# reject_if_proxy は no-op の防御ガードとして残置。


# === 認証必須 ============================================================


def test_endpoints_require_auth(client, db):
    assert client.get("/api/v1/audit-packages").status_code == 401
    assert client.get("/api/v1/audit-responses").status_code == 401
    assert client.post("/api/v1/audit-packages", json={}).status_code == 401


# === GET /keypair/<id>/public ===========================================


def test_get_other_public_key_related(client, db):
    owner, auditor = _user(db), _user(db)
    auditor.public_key = b"\x07" * 32
    db.session.commit()
    _grant(db, owner, auditor)
    _login(client, owner)
    r = client.get(f"/api/v1/keypair/{auditor.id}/public")
    assert r.status_code == 200
    assert r.get_json()["public_key"] == _b(b"\x07" * 32)


def test_get_other_public_key_unrelated_404(client, db):
    owner, auditor, stranger = _user(db), _user(db), _user(db)
    _grant(db, owner, auditor)
    _login(client, stranger)
    assert client.get(f"/api/v1/keypair/{owner.id}/public").status_code == 404


def test_get_other_public_key_revoked_grant_404(client, db):
    owner, auditor = _user(db), _user(db)
    _grant(db, owner, auditor, revoked=True)
    _login(client, owner)
    assert client.get(f"/api/v1/keypair/{auditor.id}/public").status_code == 404


def test_get_other_public_key_never_leaks_private(client, db):
    owner, auditor = _user(db), _user(db)
    auditor.public_key = b"\x07" * 32
    auditor.encrypted_private_key = b"\xbb" * 64
    auditor.private_key_iv = b"\xcc" * 12
    db.session.commit()
    _grant(db, owner, auditor)
    _login(client, owner)
    data = client.get(f"/api/v1/keypair/{auditor.id}/public").get_json()
    assert "encrypted_private_key" not in data
    assert "private_key_iv" not in data
