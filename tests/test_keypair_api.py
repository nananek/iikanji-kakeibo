"""E5 #112 PR-A: /api/v1/keypair エンドポイントのテスト。

X25519 鍵ペア (公開鍵平文 + MK ラップ秘密鍵暗号文) の保管・取得を検証する。
サーバは暗号文を預かるだけで中身は検証しない (E2EE: 平文を持たない)。
"""

from base64 import b64encode
from uuid import uuid4

import pytest

from app.extensions import limiter
from app.models import User


@pytest.fixture(autouse=True)
def _reset_rate_limits(app):
    """各テストで Flask-Limiter のカウンタをリセット (per-user 20/h の枯渇防止)。"""
    with app.app_context():
        try:
            limiter.reset()
        except Exception:
            pass
    yield


def _make_user(db, username=None):
    username = username or f"u{uuid4().hex[:8]}"
    u = User(username=username, email=f"{username}@example.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user):
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _payload(public=b"\xaa" * 32, enc=b"\xbb" * 64, iv=b"\xcc" * 12):
    return {
        "public_key": b64encode(public).decode(),
        "encrypted_private_key": b64encode(enc).decode(),
        "private_key_iv": b64encode(iv).decode(),
    }


# --- GET ----------------------------------------------------------------


def test_get_keypair_unset_returns_nulls(client, db):
    user = _make_user(db)
    _login(client, user)
    r = client.get("/api/v1/keypair")
    assert r.status_code == 200
    data = r.get_json()
    assert data["public_key"] is None
    assert data["encrypted_private_key"] is None
    assert data["private_key_iv"] is None


def test_get_keypair_requires_auth(client, db):
    r = client.get("/api/v1/keypair")
    assert r.status_code == 401


# --- PUT ----------------------------------------------------------------


def test_put_then_get_roundtrip(client, db):
    user = _make_user(db)
    _login(client, user)
    r = client.put("/api/v1/keypair", json=_payload())
    assert r.status_code == 200

    r = client.get("/api/v1/keypair")
    data = r.get_json()
    assert data["public_key"] == b64encode(b"\xaa" * 32).decode()
    assert data["encrypted_private_key"] == b64encode(b"\xbb" * 64).decode()
    assert data["private_key_iv"] == b64encode(b"\xcc" * 12).decode()


def test_put_persists_to_db(client, db):
    user = _make_user(db)
    _login(client, user)
    client.put("/api/v1/keypair", json=_payload())
    refreshed = db.session.get(User, user.id)
    assert refreshed.public_key == b"\xaa" * 32
    assert refreshed.private_key_iv == b"\xcc" * 12


def test_put_when_already_set_is_409(client, db):
    user = _make_user(db)
    _login(client, user)
    assert client.put("/api/v1/keypair", json=_payload()).status_code == 200
    # 別の鍵で上書きを試みる → 409 (鍵ペアの不可逆性を守る)
    r = client.put(
        "/api/v1/keypair",
        json=_payload(public=b"\x11" * 32, enc=b"\x22" * 64, iv=b"\x33" * 12),
    )
    assert r.status_code == 409
    refreshed = db.session.get(User, user.id)
    assert refreshed.public_key == b"\xaa" * 32  # 変わっていない


def test_put_bad_public_key_length_is_400(client, db):
    user = _make_user(db)
    _login(client, user)
    r = client.put("/api/v1/keypair", json=_payload(public=b"\xaa" * 31))
    assert r.status_code == 400
    assert db.session.get(User, user.id).public_key is None


def test_put_bad_iv_length_is_400(client, db):
    user = _make_user(db)
    _login(client, user)
    r = client.put("/api/v1/keypair", json=_payload(iv=b"\xcc" * 11))
    assert r.status_code == 400


def test_put_missing_field_is_400(client, db):
    user = _make_user(db)
    _login(client, user)
    r = client.put(
        "/api/v1/keypair",
        json={"public_key": b64encode(b"\xaa" * 32).decode()},
    )
    assert r.status_code == 400


def test_put_invalid_base64_is_400(client, db):
    user = _make_user(db)
    _login(client, user)
    payload = _payload()
    payload["public_key"] = "!!!not-base64!!!"
    r = client.put("/api/v1/keypair", json=payload)
    assert r.status_code == 400


def test_put_oversized_private_key_is_400(client, db):
    user = _make_user(db)
    _login(client, user)
    r = client.put("/api/v1/keypair", json=_payload(enc=b"\xbb" * 257))
    assert r.status_code == 400


def test_put_undersized_private_key_is_400(client, db):
    # pkcs8(48) + GCM tag(16) = 64B 未満の極小ブロブは拒否
    user = _make_user(db)
    _login(client, user)
    r = client.put("/api/v1/keypair", json=_payload(enc=b"\xbb" * 32))
    assert r.status_code == 400
    assert db.session.get(User, user.id).public_key is None


def test_put_requires_auth(client, db):
    r = client.put("/api/v1/keypair", json=_payload())
    assert r.status_code == 401


# --- ユーザー分離 -------------------------------------------------------


def test_get_returns_only_own_keypair(client, db):
    # alice の鍵ペアを直接セット (alice 経由のログインはせず DB に書く)
    alice = _make_user(db, "alice")
    alice.public_key = b"\x01" * 32
    alice.encrypted_private_key = b"\xbb" * 64
    alice.private_key_iv = b"\xcc" * 12
    db.session.commit()

    bob = _make_user(db, "bob")
    _login(client, bob)
    data = client.get("/api/v1/keypair").get_json()
    # bob は自分の (未設定) 鍵ペアしか見えない (alice のは漏れない)
    assert data["public_key"] is None
    assert data["encrypted_private_key"] is None


# 旧監査代理閲覧 (acting-as) 中の鍵ペア遮断テスト (test_put/get_rejected_during_
# proxy_view) は、リアルタイム代理閲覧の撤去 (#112) に伴い削除した。鍵ペア API は
# 常にログイン本人 (current_user) の self-service で、他ユーザーの鍵には到達でき
# ない (IDOR テストで担保)。reject_if_proxy は no-op の防御ガードとして残置。


# --- GET /<user_id>/public (監査相手の公開鍵取得) ------------------------


def _grant(db, owner, auditor, level=3, revoked=False):
    from datetime import datetime, timezone

    from app.models.audit import AuditGrant

    g = AuditGrant(
        owner_user_id=owner.id, auditor_user_id=auditor.id,
        permission_level=level, status="draft",
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    )
    db.session.add(g)
    db.session.commit()
    return g


def test_get_public_key_owner_to_auditor_returns_200(client, db):
    """owner が grant 先の auditor の公開鍵を取得できる。"""
    owner = _make_user(db, "owner_pk")
    auditor = _make_user(db, "auditor_pk")
    auditor.public_key = b"\x07" * 32
    db.session.commit()
    _grant(db, owner, auditor)

    _login(client, owner)
    r = client.get(f"/api/v1/keypair/{auditor.id}/public")
    assert r.status_code == 200
    data = r.get_json()
    assert data["user_id"] == auditor.id
    assert data["public_key"] == b64encode(b"\x07" * 32).decode()


def test_get_public_key_auditor_to_owner_returns_200(client, db):
    """auditor も同じ grant で owner の公開鍵を取得できる (双方向)。

    Flask-Login の制約上、1 テスト 1 ログインで owner→auditor と別に検証する
    (複数ログインは最初のユーザーに解決されるため。feedback_flask_login_test_context)。
    """
    owner = _make_user(db, "owner_pk2")
    owner.public_key = b"\x08" * 32
    auditor = _make_user(db, "auditor_pk2")
    db.session.commit()
    _grant(db, owner, auditor)

    _login(client, auditor)
    r = client.get(f"/api/v1/keypair/{owner.id}/public")
    assert r.status_code == 200
    data = r.get_json()
    assert data["user_id"] == owner.id
    assert data["public_key"] == b64encode(b"\x08" * 32).decode()


def test_get_public_key_peer_unset_returns_null(client, db):
    """相手がまだ鍵ペア未設定なら 200 + public_key=null (renderer の no-key パス)。"""
    owner = _make_user(db, "owner_null")
    auditor = _make_user(db, "auditor_null")  # public_key 未設定
    db.session.commit()
    _grant(db, owner, auditor)

    _login(client, owner)
    r = client.get(f"/api/v1/keypair/{auditor.id}/public")
    assert r.status_code == 200
    data = r.get_json()
    assert data["user_id"] == auditor.id
    assert data["public_key"] is None


def test_get_public_key_without_grant_is_404(client, db):
    """grant で結ばれていない相手の公開鍵は取得できない (存在を秘匿し 404)。"""
    me = _make_user(db, "me_nopub")
    other = _make_user(db, "other_nopub")
    other.public_key = b"\x07" * 32
    db.session.commit()

    _login(client, me)
    r = client.get(f"/api/v1/keypair/{other.id}/public")
    assert r.status_code == 404


def test_get_public_key_revoked_grant_is_404(client, db):
    """失効した grant では公開鍵を取得できない (§14.10)。"""
    owner = _make_user(db, "owner_rev")
    auditor = _make_user(db, "auditor_rev")
    auditor.public_key = b"\x07" * 32
    db.session.commit()
    _grant(db, owner, auditor, revoked=True)

    _login(client, owner)
    r = client.get(f"/api/v1/keypair/{auditor.id}/public")
    assert r.status_code == 404


def test_get_public_key_requires_auth(client, db):
    """未認証では公開鍵を取得できない。"""
    other = _make_user(db, "other_unauth")
    with client.session_transaction() as sess:
        sess.clear()
    r = client.get(f"/api/v1/keypair/{other.id}/public")
    assert r.status_code in (401, 403)


def test_get_public_key_never_leaks_private_key(client, db):
    """公開鍵レスポンスに秘密鍵関連フィールドが絶対に含まれない。"""
    owner = _make_user(db, "owner_leak")
    auditor = _make_user(db, "auditor_leak")
    auditor.public_key = b"\x07" * 32
    auditor.encrypted_private_key = b"\xbb" * 64
    auditor.private_key_iv = b"\xcc" * 12
    db.session.commit()
    _grant(db, owner, auditor)

    _login(client, owner)
    r = client.get(f"/api/v1/keypair/{auditor.id}/public")
    assert r.status_code == 200
    data = r.get_json()
    assert "encrypted_private_key" not in data
    assert "private_key_iv" not in data
    assert set(data.keys()) == {"user_id", "public_key"}
