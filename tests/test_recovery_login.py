"""パスキー専用モード + リカバリコードのテスト

PR-A: リカバリコード生成・1 回限り使用検証
PR-B: パスキー専用ログインモード本体 (このファイルにも追加予定)
"""

import hashlib

import pytest

from app.models.user import User, RECOVERY_CODE_PREFIX


# ============================================================
# リカバリコードモデル単体テスト
# ============================================================

class TestRecoveryCodeModel:
    """User.set_recovery_code / verify / consume の挙動"""

    def test_set_recovery_code_returns_raw_and_stores_hash(self, db, user):
        raw = user.set_recovery_code()
        db.session.commit()
        # 生コードは ikr_ プレフィックス + 64 文字 hex
        assert raw.startswith(RECOVERY_CODE_PREFIX)
        assert len(raw) == len(RECOVERY_CODE_PREFIX) + 64
        # DB には SHA256 ハッシュのみ保存
        assert user.recovery_code_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert user.recovery_code_hash != raw
        # 表示用プレフィックス
        assert user.recovery_code_prefix.startswith(RECOVERY_CODE_PREFIX)
        assert user.recovery_code_prefix.endswith("...")
        # 未使用
        assert user.recovery_code_used_at is None
        assert user.recovery_code_created_at is not None

    def test_set_recovery_code_overwrites_old(self, db, user):
        raw1 = user.set_recovery_code()
        hash1 = user.recovery_code_hash
        db.session.commit()
        raw2 = user.set_recovery_code()
        db.session.commit()
        # 新規生成すると hash が変わり、旧コードは無効
        assert raw1 != raw2
        assert user.recovery_code_hash != hash1
        # 旧 raw1 で verify しても失敗 (新コードに置き換わっている)
        assert not user.verify_recovery_code(raw1)
        # 新 raw2 では成功
        assert user.verify_recovery_code(raw2)

    def test_set_recovery_code_resets_used_at(self, db, user):
        """使用済みコードがあっても、再生成すると used_at がクリアされる"""
        user.set_recovery_code()
        user.consume_recovery_code()
        db.session.commit()
        assert user.recovery_code_used_at is not None
        user.set_recovery_code()
        db.session.commit()
        assert user.recovery_code_used_at is None

    def test_verify_recovery_code_rejects_used(self, db, user):
        raw = user.set_recovery_code()
        db.session.commit()
        assert user.verify_recovery_code(raw)
        user.consume_recovery_code()
        db.session.commit()
        assert not user.verify_recovery_code(raw)

    def test_verify_recovery_code_rejects_wrong(self, db, user):
        user.set_recovery_code()
        db.session.commit()
        assert not user.verify_recovery_code("ikr_wrong_value")
        assert not user.verify_recovery_code("")
        assert not user.verify_recovery_code(None)

    def test_has_active_recovery_code_property(self, db, user):
        assert user.has_active_recovery_code is False
        user.set_recovery_code()
        db.session.commit()
        assert user.has_active_recovery_code is True
        user.consume_recovery_code()
        db.session.commit()
        assert user.has_active_recovery_code is False


# ============================================================
# POST /settings/passkeys/recovery/generate
# ============================================================

class TestRecoveryGenerateEndpoint:
    """リカバリコード生成エンドポイントの挙動"""

    def test_unauthenticated_redirects(self, client):
        resp = client.post("/settings/passkeys/recovery/generate",
                           data={"password": "x"})
        assert resp.status_code in (302, 401)

    def test_wrong_password_rejected(self, logged_in_client, user):
        resp = logged_in_client.post(
            "/settings/passkeys/recovery/generate",
            data={"password": "wrong-password"},
        )
        # パスキー設定画面にリダイレクト + flash
        assert resp.status_code == 302
        # DB に書き込まれていない
        assert user.recovery_code_hash is None

    def test_no_password_rejected(self, logged_in_client, user):
        resp = logged_in_client.post(
            "/settings/passkeys/recovery/generate",
            data={},
        )
        assert resp.status_code == 302
        assert user.recovery_code_hash is None

    def test_correct_password_renders_raw_code(self, logged_in_client, user, db):
        # user フィクスチャのデフォルトパスワード (conftest 参照)
        resp = logged_in_client.post(
            "/settings/passkeys/recovery/generate",
            data={"password": "password123"},
        )
        assert resp.status_code == 200
        html = resp.data.decode()
        # 生コードが画面に埋め込まれている
        assert RECOVERY_CODE_PREFIX in html
        # DB にハッシュが保存されている
        db.session.refresh(user)
        assert user.recovery_code_hash is not None
        assert user.recovery_code_used_at is None

    def test_overwrites_existing_code(self, logged_in_client, user, db):
        """既存コードがあっても上書き生成される"""
        # 1 回目
        resp1 = logged_in_client.post(
            "/settings/passkeys/recovery/generate",
            data={"password": "password123"},
        )
        assert resp1.status_code == 200
        db.session.refresh(user)
        old_hash = user.recovery_code_hash

        # 2 回目
        resp2 = logged_in_client.post(
            "/settings/passkeys/recovery/generate",
            data={"password": "password123"},
        )
        assert resp2.status_code == 200
        db.session.refresh(user)
        assert user.recovery_code_hash != old_hash
