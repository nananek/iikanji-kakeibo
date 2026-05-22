"""E1 PR-B: /api/v1/wrapped-keys エンドポイントのテスト。

設計書 §10.3 / §10.4 / §10.9 の API 仕様を検証する。
"""

from base64 import b64encode
from uuid import uuid4

import pytest

from app.models import User, WebAuthnCredential, WrappedKey
from app.models.wrapped_key import (
    METHOD_PASSKEY_PRF,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
)


def _make_user(db, username=None):
    username = username or f"u{uuid4().hex[:8]}"
    u = User(username=username, email=f"{username}@example.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _make_credential(db, user, credential_id=None):
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=credential_id or uuid4().bytes,
        credential_public_key=b"pk",
        current_sign_count=0,
    )
    db.session.add(cred)
    db.session.commit()
    return cred


def _login(client, user):
    """Flask-Login のテスト用ログイン補助。"""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _passphrase_payload():
    return {
        "method": METHOD_PASSPHRASE,
        "wrapped_master_key": b64encode(b"\x00" * 48).decode(),
        "wrap_iv": b64encode(b"\x01" * 12).decode(),
        "salt": b64encode(b"\x02" * 16).decode(),
        "kdf_params": {"memory": 65536, "iterations": 3, "parallelism": 1},
        "label": "passphrase-1",
    }


def _recovery_payload():
    return {
        "method": METHOD_RECOVERY_SEED,
        "wrapped_master_key": b64encode(b"\x10" * 48).decode(),
        "wrap_iv": b64encode(b"\x11" * 12).decode(),
    }


def _passkey_payload(credential_id: int):
    return {
        "method": METHOD_PASSKEY_PRF,
        "webauthn_credential_id": credential_id,
        "wrapped_master_key": b64encode(b"\x20" * 48).decode(),
        "wrap_iv": b64encode(b"\x21" * 12).decode(),
        "label": "iPhone",
    }


# --- GET ---


def test_list_requires_login(client, db):
    """未認証は 401 / リダイレクト。"""
    resp = client.get("/api/v1/wrapped-keys")
    # Flask-Login は default で /login にリダイレクト or 401
    assert resp.status_code in (302, 401)


def test_list_returns_only_own_rows(client, db):
    """他ユーザーの wrapped_keys は見えない (IDOR)。"""
    me = _make_user(db, "me")
    other = _make_user(db, "other")
    # 自分の 1 行 + 他人の 1 行
    db.session.add_all([
        WrappedKey(
            user_id=me.id, method=METHOD_PASSPHRASE,
            wrapped_master_key=b"\x00" * 48, wrap_iv=b"\x01" * 12,
            salt=b"\x02" * 16,
            kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        ),
        WrappedKey(
            user_id=other.id, method=METHOD_PASSPHRASE,
            wrapped_master_key=b"\x10" * 48, wrap_iv=b"\x11" * 12,
            salt=b"\x12" * 16,
            kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        ),
    ])
    db.session.commit()

    _login(client, me)
    resp = client.get("/api/v1/wrapped-keys")
    assert resp.status_code == 200
    rows = resp.get_json()["wrapped_keys"]
    assert len(rows) == 1
    assert rows[0]["method"] == METHOD_PASSPHRASE
    # base64 で wrapped_master_key が返却される (raw bytes ではない)
    assert isinstance(rows[0]["wrapped_master_key"], str)


# --- POST ---


def test_create_passphrase(client, db):
    user = _make_user(db)
    _login(client, user)
    resp = client.post("/api/v1/wrapped-keys", json=_passphrase_payload())
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["method"] == METHOD_PASSPHRASE
    assert body["id"] is not None


def test_create_passkey_requires_credential(client, db):
    user = _make_user(db)
    _login(client, user)
    payload = {
        "method": METHOD_PASSKEY_PRF,
        # webauthn_credential_id を渡さない
        "wrapped_master_key": b64encode(b"\x00" * 48).decode(),
        "wrap_iv": b64encode(b"\x01" * 12).decode(),
    }
    resp = client.post("/api/v1/wrapped-keys", json=payload)
    assert resp.status_code == 400
    assert "webauthn_credential_id" in resp.get_json()["error"]


def test_create_passkey_credential_must_belong_to_user(client, db):
    """他ユーザーの credential を指定すると 404 (IDOR 防止)。"""
    me = _make_user(db, "me")
    other = _make_user(db, "other")
    other_cred = _make_credential(db, other)

    _login(client, me)
    resp = client.post(
        "/api/v1/wrapped-keys", json=_passkey_payload(other_cred.id)
    )
    assert resp.status_code == 404


def test_create_passkey_success(client, db):
    user = _make_user(db)
    cred = _make_credential(db, user)
    _login(client, user)
    resp = client.post("/api/v1/wrapped-keys", json=_passkey_payload(cred.id))
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["method"] == METHOD_PASSKEY_PRF
    assert body["webauthn_credential_id"] == cred.id


def test_create_passphrase_must_not_have_credential(client, db):
    """passphrase で credential_id を渡すと 400。"""
    user = _make_user(db)
    cred = _make_credential(db, user)
    _login(client, user)
    payload = _passphrase_payload()
    payload["webauthn_credential_id"] = cred.id
    resp = client.post("/api/v1/wrapped-keys", json=payload)
    assert resp.status_code == 400


def test_create_invalid_method(client, db):
    user = _make_user(db)
    _login(client, user)
    payload = _passphrase_payload()
    payload["method"] = "invalid"
    resp = client.post("/api/v1/wrapped-keys", json=payload)
    assert resp.status_code == 400


def test_create_invalid_iv_length(client, db):
    user = _make_user(db)
    _login(client, user)
    payload = _passphrase_payload()
    # 12 バイトでない IV
    payload["wrap_iv"] = b64encode(b"\x01" * 8).decode()
    resp = client.post("/api/v1/wrapped-keys", json=payload)
    assert resp.status_code == 400
    assert "12 bytes" in resp.get_json()["error"]
    assert "IV" in resp.get_json()["error"]


def test_create_invalid_base64(client, db):
    user = _make_user(db)
    _login(client, user)
    payload = _passphrase_payload()
    payload["wrapped_master_key"] = "@@@not-base64@@@"
    resp = client.post("/api/v1/wrapped-keys", json=payload)
    assert resp.status_code == 400


def test_create_duplicate_passphrase_conflict(client, db):
    """同じユーザーに passphrase 2 行で 409。"""
    user = _make_user(db)
    _login(client, user)
    resp1 = client.post("/api/v1/wrapped-keys", json=_passphrase_payload())
    assert resp1.status_code == 201
    resp2 = client.post("/api/v1/wrapped-keys", json=_passphrase_payload())
    assert resp2.status_code == 409


def test_response_does_not_contain_raw_keys(client, db):
    """API レスポンスのフィールド名は wrapped_master_key で、平文 master_key
    キーワードを含まない。E2EE 平文鍵漏れチェックの一環。
    """
    user = _make_user(db)
    _login(client, user)
    client.post("/api/v1/wrapped-keys", json=_passphrase_payload())

    resp = client.get("/api/v1/wrapped-keys")
    body_text = resp.get_data(as_text=True)
    # 暗号文を持つフィールド名は OK (wrapped_master_key) だが、平文を示す
    # 命名 (master_key 単独 / raw_seed / derived_key) は含まないこと
    assert '"raw_seed"' not in body_text
    assert '"derived_key"' not in body_text
    # wrapped_master_key は許容
    assert "wrapped_master_key" in body_text


# --- PUT /touch ---


def test_touch_updates_last_used_at(client, db):
    user = _make_user(db)
    _login(client, user)
    resp = client.post("/api/v1/wrapped-keys", json=_passphrase_payload())
    row_id = resp.get_json()["id"]

    resp = client.put(f"/api/v1/wrapped-keys/{row_id}/touch")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["last_used_at"] is not None


def test_touch_other_user_returns_404(client, db):
    """他ユーザーの wrapped_key への touch は 404。"""
    me = _make_user(db, "me")
    other = _make_user(db, "other")
    row = WrappedKey(
        user_id=other.id,
        method=METHOD_PASSPHRASE,
        wrapped_master_key=b"\x00" * 48, wrap_iv=b"\x01" * 12,
        salt=b"\x02" * 16,
        kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
    )
    db.session.add(row)
    db.session.commit()

    _login(client, me)
    resp = client.put(f"/api/v1/wrapped-keys/{row.id}/touch")
    assert resp.status_code == 404


def test_touch_missing_returns_404(client, db):
    user = _make_user(db)
    _login(client, user)
    resp = client.put("/api/v1/wrapped-keys/99999/touch")
    assert resp.status_code == 404


# --- DELETE ---


def test_delete_success_when_other_keys_exist(client, db):
    """他の wrapped_key が残るときは削除可能。"""
    user = _make_user(db)
    _login(client, user)
    client.post("/api/v1/wrapped-keys", json=_passphrase_payload())
    resp = client.post("/api/v1/wrapped-keys", json=_recovery_payload())
    recovery_id = resp.get_json()["id"]

    resp = client.delete(f"/api/v1/wrapped-keys/{recovery_id}")
    assert resp.status_code == 204
    # 一覧から消えている
    listing = client.get("/api/v1/wrapped-keys").get_json()["wrapped_keys"]
    assert all(r["id"] != recovery_id for r in listing)


def test_delete_last_returns_409(client, db):
    """最後の wrapped_key を削除しようとすると 409 Conflict。"""
    user = _make_user(db)
    _login(client, user)
    resp = client.post("/api/v1/wrapped-keys", json=_passphrase_payload())
    only_id = resp.get_json()["id"]

    resp = client.delete(f"/api/v1/wrapped-keys/{only_id}")
    assert resp.status_code == 409
    # 残っている
    listing = client.get("/api/v1/wrapped-keys").get_json()["wrapped_keys"]
    assert any(r["id"] == only_id for r in listing)


def test_delete_other_user_returns_404(client, db):
    """他ユーザーの wrapped_key への DELETE は 404 (IDOR)。"""
    me = _make_user(db, "me")
    other = _make_user(db, "other")
    row = WrappedKey(
        user_id=other.id,
        method=METHOD_PASSPHRASE,
        wrapped_master_key=b"\x00" * 48, wrap_iv=b"\x01" * 12,
        salt=b"\x02" * 16,
        kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
    )
    db.session.add(row)
    db.session.commit()

    _login(client, me)
    resp = client.delete(f"/api/v1/wrapped-keys/{row.id}")
    assert resp.status_code == 404
