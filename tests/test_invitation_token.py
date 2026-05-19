"""招待トークン (Phase 8 #72)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models.invitation import InvitationToken
from app.models.user import User


@pytest.fixture
def reset_limiter(app):
    try:
        from app.extensions import limiter
        limiter.reset()
    except Exception:
        pass
    yield


class TestInvitationTokenModel:
    def test_generate_returns_raw_and_record(self, db):
        raw, record = InvitationToken.generate("alice@example.com")
        assert isinstance(raw, str)
        assert len(raw) > 20  # secrets.token_urlsafe(32) は ~43 文字
        assert record.email == "alice@example.com"
        assert record.token_hash != raw  # ハッシュ済 != raw
        assert len(record.token_hash) == 64  # SHA-256 hex

    def test_find_valid_returns_record(self, db):
        raw, record = InvitationToken.generate("bob@example.com")
        db.session.add(record)
        db.session.commit()

        found = InvitationToken.find_valid(raw)
        assert found is not None
        assert found.id == record.id

    def test_find_valid_wrong_token_returns_none(self, db):
        assert InvitationToken.find_valid("nonexistent") is None
        assert InvitationToken.find_valid("") is None

    def test_expired_token_invalid(self, db):
        raw, record = InvitationToken.generate("c@example.com")
        record.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.session.add(record)
        db.session.commit()

        assert InvitationToken.find_valid(raw) is None
        assert record.is_valid() is False

    def test_used_token_invalid(self, db, user):
        raw, record = InvitationToken.generate("d@example.com")
        record.used_at = datetime.now(timezone.utc)
        record.used_by = user.id
        db.session.add(record)
        db.session.commit()

        assert InvitationToken.find_valid(raw) is None
        assert record.is_valid() is False

    def test_mark_used_updates_state(self, db, user):
        raw, record = InvitationToken.generate("e@example.com")
        db.session.add(record)
        db.session.commit()

        record.mark_used(user_id=user.id)
        db.session.commit()
        assert record.used_at is not None
        assert record.used_by == user.id
        assert record.is_valid() is False


class TestRegisterWithInvitationRequired:
    """REGISTRATION_INVITE_ONLY=True 時の挙動."""

    def test_get_without_token_returns_404(
        self, client, app, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        resp = client.get("/register")
        assert resp.status_code == 404

    def test_get_with_token_renders_form(
        self, client, app, db, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        raw, record = InvitationToken.generate("inviteme@example.com")
        db.session.add(record)
        db.session.commit()

        resp = client.get(f"/register?token={raw}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        # hidden token フィールドが埋め込まれている
        assert f'value="{raw}"' in body

    def test_post_without_token_fails(
        self, client, app, db, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        resp = client.post("/register", data={
            "username": "x", "email": "x@example.com",
            "password": "password123", "password_confirm": "password123",
            "accept_terms": "y",
        })
        # トークンなし → form 再描画 (200) + User 作成されない
        assert resp.status_code == 200
        assert User.query.count() == 0

    def test_post_with_email_mismatch_fails(
        self, client, app, db, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        raw, record = InvitationToken.generate("alice@example.com")
        db.session.add(record)
        db.session.commit()

        # 違う email で登録試行
        resp = client.post("/register", data={
            "username": "x", "email": "different@example.com",
            "password": "password123", "password_confirm": "password123",
            "accept_terms": "y", "token": raw,
        })
        assert resp.status_code == 200
        assert User.query.count() == 0

    def test_post_with_valid_token_succeeds(
        self, client, app, db, account_types, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        raw, record = InvitationToken.generate("welcome@example.com")
        db.session.add(record)
        db.session.commit()
        token_id = record.id

        resp = client.post("/register", data={
            "username": "welcome",
            "email": "welcome@example.com",
            "password": "password123",
            "password_confirm": "password123",
            "accept_terms": "y", "token": raw,
        })
        # 登録成功 → login へリダイレクト
        assert resp.status_code == 302
        # User 作成済
        user = User.query.filter_by(email="welcome@example.com").first()
        assert user is not None
        # トークンが使用済マーク
        updated = db.session.get(InvitationToken, token_id)
        assert updated.used_at is not None
        assert updated.used_by == user.id

    def test_post_with_used_token_fails(
        self, client, app, db, user, monkeypatch, reset_limiter,
    ):
        """既に使用済の token は受理されない."""
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        raw, record = InvitationToken.generate("once@example.com")
        record.used_at = datetime.now(timezone.utc)
        record.used_by = user.id
        db.session.add(record)
        db.session.commit()

        resp = client.post("/register", data={
            "username": "x", "email": "once@example.com",
            "password": "password123", "password_confirm": "password123",
            "accept_terms": "y", "token": raw,
        })
        assert resp.status_code == 200
        assert User.query.filter_by(email="once@example.com").count() == 0

    def test_personal_token_rejected_on_auditor_register(
        self, client, app, db, monkeypatch, reset_limiter,
    ):
        """personal トークンで register_auditor は使えない."""
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", True)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        raw, record = InvitationToken.generate(
            "mixed@example.com", user_type="personal",
        )
        db.session.add(record)
        db.session.commit()

        resp = client.post("/register/auditor", data={
            "username": "x", "email": "mixed@example.com",
            "password": "password123", "password_confirm": "password123",
            "accept_terms": "y", "token": raw,
        })
        # user_type 不一致で 200 再描画
        assert resp.status_code == 200
        assert User.query.filter_by(email="mixed@example.com").count() == 0


class TestRegisterWithoutInviteMode:
    """REGISTRATION_INVITE_ONLY=False (デフォルト) は従来通り動作."""

    def test_get_works_without_token(
        self, client, app, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", False)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        resp = client.get("/register")
        assert resp.status_code == 200

    def test_post_succeeds_without_token(
        self, client, app, db, account_types, monkeypatch, reset_limiter,
    ):
        monkeypatch.setitem(app.config, "REGISTRATION_INVITE_ONLY", False)
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        resp = client.post("/register", data={
            "username": "free",
            "email": "free@example.com",
            "password": "password123",
            "password_confirm": "password123",
            "accept_terms": "y",
        })
        assert resp.status_code == 302
        assert User.query.filter_by(email="free@example.com").count() == 1
