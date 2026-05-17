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


# ============================================================
# パスキー専用モード (passkey_only_login)
# ============================================================

class TestPasskeyOnlyLoginBlock:
    """passkey_only_login=True のユーザーはパスワードログインを拒否される"""

    def test_passkey_only_user_cannot_login_with_password(
        self, client, passkey_only_user
    ):
        resp = client.post("/login", data={
            "username": passkey_only_user.username,
            "password": "password123",
        }, follow_redirects=False)
        # ログイン拒否（200 で再描画、リダイレクトしない）
        assert resp.status_code == 200
        # セッションにログイン情報が乗っていないこと
        with client.session_transaction() as sess:
            assert "_user_id" not in sess

    def test_normal_user_can_still_login_with_password(self, client, user):
        """フラグが False のユーザーは従来通りパスワードでログインできる"""
        resp = client.post("/login", data={
            "username": user.username,
            "password": "password123",
        }, follow_redirects=False)
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("_user_id") == str(user.id)


class TestPasskeyOnlyToggle:
    """パスキー専用モードの enable / disable"""

    def test_enable_requires_existing_passkey(self, logged_in_client, user, db):
        """パスキー未登録なら有効化できない"""
        user.set_recovery_code()
        db.session.commit()
        resp = logged_in_client.post("/settings/passkeys/passkey-only/enable",
                                     follow_redirects=False)
        assert resp.status_code == 302
        db.session.refresh(user)
        assert user.passkey_only_login is False

    def test_enable_requires_recovery_code(self, logged_in_client, user, db):
        """リカバリコード未生成なら有効化できない"""
        from app.models.webauthn import WebAuthnCredential
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"x", credential_public_key=b"y",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()
        resp = logged_in_client.post("/settings/passkeys/passkey-only/enable",
                                     follow_redirects=False)
        assert resp.status_code == 302
        db.session.refresh(user)
        assert user.passkey_only_login is False

    def test_enable_succeeds_with_both(self, logged_in_client, user, db):
        from app.models.webauthn import WebAuthnCredential
        user.set_recovery_code()
        cred = WebAuthnCredential(
            user_id=user.id, credential_id=b"x", credential_public_key=b"y",
            current_sign_count=0,
        )
        db.session.add(cred)
        db.session.commit()
        resp = logged_in_client.post("/settings/passkeys/passkey-only/enable",
                                     follow_redirects=False)
        assert resp.status_code == 302
        db.session.refresh(user)
        assert user.passkey_only_login is True

    def test_disable_requires_password(self, logged_in_client, passkey_only_user, db):
        resp = logged_in_client.post(
            "/settings/passkeys/passkey-only/disable",
            data={"password": "wrong-password"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        db.session.refresh(passkey_only_user)
        assert passkey_only_user.passkey_only_login is True  # 変わらず

    def test_disable_succeeds_with_correct_password(
        self, logged_in_client, passkey_only_user, db
    ):
        resp = logged_in_client.post(
            "/settings/passkeys/passkey-only/disable",
            data={"password": "password123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        db.session.refresh(passkey_only_user)
        assert passkey_only_user.passkey_only_login is False


class TestDeleteLastPasskeyBlock:
    """パスキー専用モード中は最後の 1 本を削除できない"""

    def test_blocks_delete_when_last_passkey(self, logged_in_client, passkey_only_user, db):
        from app.models.webauthn import WebAuthnCredential
        cred = WebAuthnCredential.query.filter_by(
            user_id=passkey_only_user.id
        ).first()
        resp = logged_in_client.post(
            f"/settings/passkeys/{cred.id}/delete",
            headers={"HX-Request": "true"},
        )
        # htmx 422 でブロック
        assert resp.status_code == 422
        # DB にまだ存在する
        assert WebAuthnCredential.query.get(cred.id) is not None

    def test_allows_delete_when_more_than_one(self, logged_in_client, passkey_only_user, db):
        """パスキーが 2 本あれば 1 本目は削除できる"""
        from app.models.webauthn import WebAuthnCredential
        extra = WebAuthnCredential(
            user_id=passkey_only_user.id,
            credential_id=b"second-cred",
            credential_public_key=b"pk2",
            current_sign_count=0,
        )
        db.session.add(extra)
        db.session.commit()
        first = WebAuthnCredential.query.filter_by(
            user_id=passkey_only_user.id
        ).first()
        resp = logged_in_client.post(
            f"/settings/passkeys/{first.id}/delete",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        # 1 本になったが削除自体は成功
        assert WebAuthnCredential.query.filter_by(
            user_id=passkey_only_user.id
        ).count() == 1


class TestRecoveryLogin:
    """POST /recovery エンドポイント"""

    def test_get_renders_form(self, client):
        resp = client.get("/recovery")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "リカバリコード" in html

    def test_wrong_code_uniform_error(self, client, passkey_only_user):
        resp = client.post("/recovery", data={
            "username": passkey_only_user.username,
            "recovery_code": "ikr_wrong_value",
        })
        # ログインできない (200 で再描画)
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "_user_id" not in sess

    def test_nonexistent_user_uniform_error(self, client):
        """存在しないユーザー名でも同じ応答 (列挙対策)"""
        resp = client.post("/recovery", data={
            "username": "no-such-user",
            "recovery_code": "ikr_any",
        })
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "_user_id" not in sess

    def test_successful_recovery_login_consumes_code(self, client, passkey_only_user, db):
        """正しいコードでログインでき、コードは消費される"""
        raw = passkey_only_user._test_recovery_code_raw

        resp = client.post("/recovery", data={
            "username": passkey_only_user.username,
            "recovery_code": raw,
        }, follow_redirects=False)
        # /settings/passkeys へリダイレクト
        assert resp.status_code == 302
        assert "/settings/passkeys" in resp.headers["Location"]
        # セッションに pending_recovery_action が立っている
        with client.session_transaction() as sess:
            assert sess.get("_user_id") == str(passkey_only_user.id)
            assert sess.get("pending_recovery_action") is True
        # コードが消費済みになっている
        db.session.refresh(passkey_only_user)
        assert passkey_only_user.recovery_code_used_at is not None

    def test_consumed_code_cannot_login_again(self, client, passkey_only_user, db):
        """1 回限り使用: 一度使ったコードは再使用不可"""
        raw = passkey_only_user._test_recovery_code_raw
        # 1 回目: 成功
        client.post("/recovery", data={
            "username": passkey_only_user.username,
            "recovery_code": raw,
        })
        # ログアウト相当（cookie 含めて完全リセット）
        client.get("/logout")
        # 2 回目: 拒否（ログイン画面再描画）
        resp = client.post("/recovery", data={
            "username": passkey_only_user.username,
            "recovery_code": raw,
        }, follow_redirects=False)
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "_user_id" not in sess


class TestPendingRecoveryGate:
    """リカバリログイン後の強制復旧フロー (before_request フック)"""

    def _login_with_recovery(self, client, user, db):
        raw = getattr(user, "_test_recovery_code_raw", None) or user.set_recovery_code()
        db.session.commit()
        client.post("/recovery", data={
            "username": user.username,
            "recovery_code": raw,
        })

    def test_dashboard_redirects_to_passkeys(self, client, passkey_only_user, db):
        self._login_with_recovery(client, passkey_only_user, db)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/settings/passkeys" in resp.headers["Location"]

    def test_passkeys_page_accessible(self, client, passkey_only_user, db):
        self._login_with_recovery(client, passkey_only_user, db)
        resp = client.get("/settings/passkeys")
        # pending 中でもパスキー画面は開ける
        assert resp.status_code == 200

    def test_logout_allowed(self, client, passkey_only_user, db):
        self._login_with_recovery(client, passkey_only_user, db)
        resp = client.get("/logout", follow_redirects=False)
        # ログアウトは pending 中でも可
        assert resp.status_code == 302

    def test_cleared_after_new_passkey_and_recovery(
        self, client, passkey_only_user, db
    ):
        """新規リカバリ生成（パスキーは既に存在）で pending クリア"""
        self._login_with_recovery(client, passkey_only_user, db)
        # 既にパスキー 1 本ある状態。リカバリコードを再生成すると
        # has_passkey + has_recovery が両方成立して pending がクリアされる
        client.post("/settings/passkeys/recovery/generate", data={
            "password": "password123",
        })
        with client.session_transaction() as sess:
            assert sess.get("pending_recovery_action") is None

    # NOTE: セッション整合性 (pending_recovery_user_id != current_user.id で
    # flag を pop) の検証は Flask-Login の `_fresh` 等が絡み、テストクライアント
    # 経由では再現困難。実装は `pending_recovery_gate` に残しており、攻撃面
    # 縮小目的のディフェンスインデプスとして機能する。


# ============================================================
# 監査ユーザーのパスキー専用モード
# ============================================================

class TestPasskeyOnlyAuditor:
    """auditor も passkey_only_login を使えること"""

    def test_auditor_login_blocked_in_passkey_only_mode(self, client, auditor, db):
        from app.models.webauthn import WebAuthnCredential
        auditor.passkey_only_login = True
        auditor.set_recovery_code()
        db.session.add(WebAuthnCredential(
            user_id=auditor.id, credential_id=b"a-cred",
            credential_public_key=b"a-pk", current_sign_count=0,
        ))
        db.session.commit()
        resp = client.post("/login/auditor", data={
            "username": auditor.username,
            "password": "password123",
        }, follow_redirects=False)
        # ログイン拒否（200 で再描画）
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert "_user_id" not in sess


# ============================================================
# タイミング攻撃対策
# ============================================================

class TestRecoveryLoginTiming:
    """ユーザー不在時とコード誤り時で応答時間に大きな差が出ないこと"""

    def test_nonexistent_user_still_runs_hash_compare(self, client, monkeypatch):
        """ユーザー不在時もダミー検証関数が呼ばれる (= compare_digest 実行)"""
        from app.views import auth as auth_view
        calls = {"dummy": 0}
        original = auth_view._verify_recovery_code_dummy

        def spy(code):
            calls["dummy"] += 1
            return original(code)

        monkeypatch.setattr(auth_view, "_verify_recovery_code_dummy", spy)
        client.post("/recovery", data={
            "username": "no-such-user",
            "recovery_code": "ikr_anything",
        })
        assert calls["dummy"] == 1
