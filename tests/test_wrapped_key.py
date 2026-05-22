"""E2EE Phase E1 (#108) の wrapped_keys モデルテスト。

設計書 §10.1 のテーブル制約 (CHECK / UNIQUE partial index / FK CASCADE) と
モデルの基本動作を検証する。
"""

from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import User, WebAuthnCredential, WrappedKey
from app.models.wrapped_key import (
    ALLOWED_METHODS,
    METHOD_PASSKEY_PRF,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY_SEED,
)


def _make_user(db, username=None):
    # UUID で衝突を回避 (パラレル実行 or テスト順序変更時の保険)
    username = username or f"u{uuid4().hex[:8]}"
    u = User(username=username, email=f"{username}@example.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _make_credential(db, user, credential_id=b"cred-1"):
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=credential_id,
        credential_public_key=b"pk",
        current_sign_count=0,
    )
    db.session.add(cred)
    db.session.commit()
    return cred


def test_create_passphrase_row(db):
    user = _make_user(db)
    row = WrappedKey(
        user_id=user.id,
        method=METHOD_PASSPHRASE,
        wrapped_master_key=b"\x00" * 48,
        wrap_iv=b"\x01" * 12,
        salt=b"\x02" * 16,
        kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
    )
    db.session.add(row)
    db.session.commit()

    fetched = WrappedKey.query.filter_by(user_id=user.id).first()
    assert fetched.method == METHOD_PASSPHRASE
    assert fetched.wrapped_master_key == b"\x00" * 48
    assert fetched.wrap_iv == b"\x01" * 12
    assert fetched.salt == b"\x02" * 16
    assert fetched.kdf_params == {"memory": 65536, "iterations": 3, "parallelism": 1}
    assert fetched.created_at is not None
    assert fetched.last_used_at is None
    assert fetched.webauthn_credential_id is None


def test_create_passkey_prf_row(db):
    user = _make_user(db)
    cred = _make_credential(db, user)
    row = WrappedKey(
        user_id=user.id,
        method=METHOD_PASSKEY_PRF,
        webauthn_credential_id=cred.id,
        wrapped_master_key=b"\x10" * 48,
        wrap_iv=b"\x11" * 12,
        label="iPhone 14 Pro",
    )
    db.session.add(row)
    db.session.commit()

    assert row.webauthn_credential is cred
    assert row.label == "iPhone 14 Pro"


def test_create_recovery_seed_row(db):
    user = _make_user(db)
    row = WrappedKey(
        user_id=user.id,
        method=METHOD_RECOVERY_SEED,
        wrapped_master_key=b"\x20" * 48,
        wrap_iv=b"\x21" * 12,
    )
    db.session.add(row)
    db.session.commit()

    assert row.method == METHOD_RECOVERY_SEED
    assert row.salt is None  # BIP-39 mnemonic 自体が高エントロピー、salt 不要


def test_method_validates_rejects_invalid(db):
    """method は @validates で値域 (ALLOWED_METHODS) を強制される。

    PostgreSQL 本番では CHECK 制約も効くが、SQLite テストでも同じ動作を
    保証するため SQLAlchemy validator で弾く。
    """
    user = _make_user(db)
    with pytest.raises(ValueError, match="method must be one of"):
        WrappedKey(
            user_id=user.id,
            method="invalid_method",
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
        )


def test_passkey_prf_requires_credential_id(db):
    """method=passkey_prf で credential_id=NULL は弾かれる。"""
    user = _make_user(db)
    cred = _make_credential(db, user)
    # まず credential_id をセット → 後から method を passkey_prf に
    row = WrappedKey(
        user_id=user.id,
        webauthn_credential_id=cred.id,
        method=METHOD_PASSKEY_PRF,
        wrapped_master_key=b"\x00" * 48,
        wrap_iv=b"\x01" * 12,
    )
    db.session.add(row)
    db.session.commit()
    assert row.id is not None

    # credential_id=NULL を passkey_prf に設定しようとすると ValueError
    with pytest.raises(ValueError, match="requires webauthn_credential_id"):
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSKEY_PRF,
            webauthn_credential_id=None,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
        )


def test_passphrase_must_not_have_credential_id(db):
    """method=passphrase / recovery_seed に credential_id をセットすると弾く。"""
    user = _make_user(db)
    cred = _make_credential(db, user)
    # method を先にセット → 後で credential_id を入れる
    with pytest.raises(ValueError, match="must not have webauthn_credential_id"):
        row = WrappedKey(
            user_id=user.id,
            method=METHOD_PASSPHRASE,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
            salt=b"\x02" * 16,
            kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        )
        row.webauthn_credential_id = cred.id

    # recovery_seed も同様
    with pytest.raises(ValueError, match="must not have webauthn_credential_id"):
        row2 = WrappedKey(
            user_id=user.id,
            method=METHOD_RECOVERY_SEED,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
        )
        row2.webauthn_credential_id = cred.id


def test_allowed_methods_constants(db):
    """ALLOWED_METHODS の 3 値が正しい定義になっている。"""
    assert METHOD_PASSKEY_PRF == "passkey_prf"
    assert METHOD_PASSPHRASE == "passphrase"
    assert METHOD_RECOVERY_SEED == "recovery_seed"
    assert set(ALLOWED_METHODS) == {"passkey_prf", "passphrase", "recovery_seed"}


def test_unique_passkey_per_credential(db):
    """同じ (user, credential) で 2 行作れない。"""
    user = _make_user(db)
    cred = _make_credential(db, user)
    db.session.add(
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSKEY_PRF,
            webauthn_credential_id=cred.id,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
        )
    )
    db.session.commit()

    db.session.add(
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSKEY_PRF,
            webauthn_credential_id=cred.id,
            wrapped_master_key=b"\x10" * 48,
            wrap_iv=b"\x11" * 12,
        )
    )
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_multiple_passkeys_per_user(db):
    """異なる Passkey credential なら同じ user で複数行 OK。"""
    user = _make_user(db)
    c1 = _make_credential(db, user, credential_id=b"cred-1")
    c2 = _make_credential(db, user, credential_id=b"cred-2")

    db.session.add_all([
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSKEY_PRF,
            webauthn_credential_id=c1.id,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
        ),
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSKEY_PRF,
            webauthn_credential_id=c2.id,
            wrapped_master_key=b"\x10" * 48,
            wrap_iv=b"\x11" * 12,
        ),
    ])
    db.session.commit()

    rows = WrappedKey.query.filter_by(user_id=user.id).all()
    assert len(rows) == 2


def test_cascade_on_webauthn_credential_delete(db):
    """webauthn_credentials 削除で wrapped_keys 行も CASCADE 削除される。"""
    user = _make_user(db)
    cred = _make_credential(db, user)
    row = WrappedKey(
        user_id=user.id,
        method=METHOD_PASSKEY_PRF,
        webauthn_credential_id=cred.id,
        wrapped_master_key=b"\x00" * 48,
        wrap_iv=b"\x01" * 12,
    )
    db.session.add(row)
    db.session.commit()
    row_id = row.id

    db.session.delete(cred)
    db.session.commit()

    assert db.session.get(WrappedKey, row_id) is None


def test_cascade_on_user_delete(db):
    """User 削除で wrapped_keys 行も CASCADE 削除される。"""
    user = _make_user(db)
    db.session.add(
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSPHRASE,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
            salt=b"\x02" * 16,
            kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        )
    )
    db.session.commit()

    user_id = user.id
    db.session.delete(user)
    db.session.commit()

    assert WrappedKey.query.filter_by(user_id=user_id).count() == 0


def test_user_relationship_backref(db):
    """user.wrapped_keys backref が動作する。"""
    user = _make_user(db)
    db.session.add_all([
        WrappedKey(
            user_id=user.id,
            method=METHOD_PASSPHRASE,
            wrapped_master_key=b"\x00" * 48,
            wrap_iv=b"\x01" * 12,
            salt=b"\x02" * 16,
            kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        ),
        WrappedKey(
            user_id=user.id,
            method=METHOD_RECOVERY_SEED,
            wrapped_master_key=b"\x10" * 48,
            wrap_iv=b"\x11" * 12,
        ),
    ])
    db.session.commit()

    rows = user.wrapped_keys.all()
    methods = sorted(r.method for r in rows)
    assert methods == [METHOD_PASSPHRASE, METHOD_RECOVERY_SEED]


def test_user_e2ee_columns_defaults(db):
    """User の E2EE 関連カラムのデフォルト値。"""
    user = _make_user(db)
    assert user.is_active is True
    assert user.migration_temp_mk is None
    assert user.public_key is None
    assert user.mk_rotation_state is None


def test_inactive_user_blocked_by_flask_login(client, db):
    """User.is_active=False のとき Flask-Login が UserMixin.is_active を
    DB カラムで上書きし、login_user() がそのユーザーを拒否することを確認。

    認証フロー全体ではなく、UserMixin.is_active 経由のセッション維持 (auth ミドル
    ウェア) でロックされることを確認 (§16.5 の鍵未設定ユーザーロック)。
    """
    user = _make_user(db, username="locked-user")
    user.is_active = False
    db.session.commit()

    # DB カラムが UserMixin プロパティを上書きしているか直接確認
    assert user.is_active is False
    # Flask-Login の get_id() は呼べる (削除されていない) が、login_user 経由
    # では is_active=False で session に乗らない (UserMixin.is_active を参照)
    from flask_login import login_user

    with client.application.test_request_context():
        result = login_user(user)
        assert result is False  # 非アクティブユーザーは login 拒否


def test_user_mk_rotation_state_jsonb(db):
    """mk_rotation_state は JSON で読み書きできる。"""
    user = _make_user(db)
    user.mk_rotation_state = {
        "status": "rotating",
        "progress": {"total": 100, "done": 23},
    }
    db.session.commit()

    fetched = db.session.get(User, user.id)
    assert fetched.mk_rotation_state["status"] == "rotating"
    assert fetched.mk_rotation_state["progress"]["done"] == 23
