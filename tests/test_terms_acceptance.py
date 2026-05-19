"""利用規約への同意フロー (Phase 1 #66 続編)。

- 登録時の同意チェックボックスが必須 (未チェックなら登録拒否)
- 登録成功時に `accepted_terms_version` が現行バージョンで記録される
- 既存ユーザー (NULL or 古いバージョン) は再同意画面に強制リダイレクト
- 同意画面 / ログアウト / 法的文書ページは再同意なしでも閲覧可
- 再同意 POST 後は通常画面に戻り、`accepted_terms_version` が更新される
"""

import pytest

from app.models.user import User


CURRENT_VERSION = "2026-05-19"


@pytest.fixture
def reset_limiter(app):
    """共有 app の rate limiter 内部状態をリセットする。
    register エンドポイントは 5/minute なので、他テストの累積分で 429 が
    返るのを防ぐ。
    """
    try:
        from app.extensions import limiter
        limiter.reset()
    except Exception:
        pass
    yield


class TestRegisterWithTermsAcceptance:
    """登録時の同意チェックボックス挙動"""

    def test_register_requires_accept_terms(
        self, client, db, account_types, app, monkeypatch, reset_limiter
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        resp = client.post(
            "/register",
            data={
                "username": "noconsent",
                "email": "noconsent@example.com",
                "password": "password123",
                "password_confirm": "password123",
                # accept_terms を送らない
            },
        )
        # フォーム再描画 (登録されず)
        assert resp.status_code == 200
        assert User.query.filter_by(username="noconsent").first() is None

    def test_register_with_accept_terms_records_version(
        self, client, db, account_types, app, monkeypatch, reset_limiter
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        resp = client.post(
            "/register",
            data={
                "username": "consenting",
                "email": "consent@example.com",
                "password": "password123",
                "password_confirm": "password123",
                "accept_terms": "y",
            },
        )
        assert resp.status_code in (302, 303)
        user = User.query.filter_by(username="consenting").first()
        assert user is not None
        assert user.accepted_terms_version == CURRENT_VERSION

    def test_register_auditor_requires_accept_terms(self, client, db, app, monkeypatch, reset_limiter):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        resp = client.post(
            "/register/auditor",
            data={
                "username": "auditor_nc",
                "email": "auditor_nc@example.com",
                "password": "password123",
                "password_confirm": "password123",
            },
        )
        assert resp.status_code == 200
        assert User.query.filter_by(username="auditor_nc").first() is None

    def test_register_auditor_with_accept_terms(self, client, db, app, monkeypatch, reset_limiter):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        resp = client.post(
            "/register/auditor",
            data={
                "username": "auditor_c",
                "email": "auditor_c@example.com",
                "password": "password123",
                "password_confirm": "password123",
                "accept_terms": "y",
            },
        )
        assert resp.status_code in (302, 303)
        user = User.query.filter_by(username="auditor_c").first()
        assert user is not None
        assert user.user_type == "auditor"
        assert user.accepted_terms_version == CURRENT_VERSION


class TestReConsentFlow:
    """既存ユーザー向け再同意フロー"""

    def test_user_with_null_terms_is_redirected(
        self, client, db, user, app, monkeypatch
    ):
        """新しい CURRENT_TERMS_VERSION 下で accepted_terms_version=NULL の
        既存ユーザーがダッシュボードにアクセスすると同意画面に飛ぶ"""
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = None
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/accept-terms" in resp.headers.get("Location", "")

    def test_user_with_old_version_is_redirected(
        self, client, db, user, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = "2026-01-01"  # 古いバージョン
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 303)
        assert "/accept-terms" in resp.headers.get("Location", "")

    def test_user_with_current_version_passes(
        self, client, db, user, accounts, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = CURRENT_VERSION
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.get("/", follow_redirects=False)
        # 同意済なのでダッシュボード (200) または別のリダイレクト
        # ただし accept-terms へのリダイレクトは出ない
        if resp.status_code in (302, 303):
            assert "/accept-terms" not in resp.headers.get("Location", "")

    def test_legal_pages_accessible_without_consent(
        self, client, db, user, app, monkeypatch
    ):
        """同意してない既存ユーザーでも法的文書は閲覧可 (規約を読めないと困る)"""
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = None
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        for slug in ("terms", "privacy", "tokushoho"):
            resp = client.get(f"/legal/{slug}", follow_redirects=False)
            assert resp.status_code == 200, f"/legal/{slug} should be accessible"

    def test_logout_accessible_without_consent(
        self, client, db, user, app, monkeypatch
    ):
        """拒否してログアウトする選択肢を残す"""
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = None
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.get("/logout", follow_redirects=False)
        # logout は 302 でログイン画面へ (accept-terms へではない)
        assert resp.status_code in (302, 303)
        assert "/accept-terms" not in resp.headers.get("Location", "")

    def test_accept_terms_post_updates_version(
        self, client, db, user, app, monkeypatch
    ):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = None
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.post(
            "/accept-terms",
            data={"accept_terms": "1"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        db.session.refresh(user)
        assert user.accepted_terms_version == CURRENT_VERSION

    def test_accept_terms_post_without_checkbox_stays(
        self, client, db, user, app, monkeypatch
    ):
        """同意チェックなしの POST では accepted_terms_version は更新されない"""
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", CURRENT_VERSION)
        user.accepted_terms_version = None
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.post("/accept-terms", data={}, follow_redirects=False)
        assert resp.status_code == 200  # 再表示
        db.session.refresh(user)
        assert user.accepted_terms_version is None


class TestTermsAcceptanceDisabled:
    """`CURRENT_TERMS_VERSION` が空文字なら同意管理は無効"""

    def test_empty_version_does_not_redirect(self, client, db, user, accounts, app, monkeypatch):
        monkeypatch.setitem(app.config, "CURRENT_TERMS_VERSION", "")
        user.accepted_terms_version = None
        db.session.commit()

        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)

        resp = client.get("/", follow_redirects=False)
        if resp.status_code in (302, 303):
            assert "/accept-terms" not in resp.headers.get("Location", "")
