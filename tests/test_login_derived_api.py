"""#385 PR-2: ログイン派生 MK の 2 ラウンドログイン API テスト。

設計書 docs/v5-e2ee/login-derived-mk.md §3.2 / §3.5。

login_verifier はサーバ側では「HMAC して保存 → 再照合」されるだけで派生の正しさは
検証されないため、テストでは任意の 32B を用いる (Argon2id/HKDF の実値は不要)。
"""

import hmac
import hashlib
from base64 import b64decode, b64encode

import pytest

from app.models.user import User
from app.models.wrapped_key import METHOD_PASSPHRASE, WrappedKey


LOGIN_SECRET = "test-login-server-secret"


@pytest.fixture(autouse=True)
def _login_secret(app):
    """このモジュールのテスト中だけ LOGIN_SERVER_SECRET を設定する。"""
    prev = app.config.get("LOGIN_SERVER_SECRET", "")
    app.config["LOGIN_SERVER_SECRET"] = LOGIN_SECRET
    yield
    app.config["LOGIN_SERVER_SECRET"] = prev


def _b64(raw):
    return b64encode(raw).decode("ascii")


def _verifier(byte=0xAA):
    return bytes([byte]) * 32


def _expected_hash(login_verifier):
    return hmac.new(
        LOGIN_SECRET.encode(), b"login-hash\x00" + login_verifier, hashlib.sha256,
    ).digest()


def _begin(client, username):
    return client.post("/auth/login/begin", json={"username": username})


def _migrate(client, username, login_verifier, salt_b64, **overrides):
    payload = {
        "username": username,
        "password": "password123",
        "login_verifier": _b64(login_verifier),
        "login_salt": salt_b64,
        "login_kdf_params": {"memory": 65536, "iterations": 3, "parallelism": 1},
        "wrapped_master_key": _b64(b"\x01" * 48),
        "wrap_iv": _b64(b"\x02" * 12),
    }
    payload.update(overrides)
    return client.post("/auth/login/finish", json=payload)


# ------------------------------------------------------------------
# /begin
# ------------------------------------------------------------------

class TestLoginBegin:
    def test_unconfigured_returns_503(self, app, client, db, user):
        app.config["LOGIN_SERVER_SECRET"] = ""
        resp = _begin(client, "testuser")
        assert resp.status_code == 503

    def test_v4_user_migration_required(self, client, db, user):
        resp = _begin(client, "testuser")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["migration_required"] is True
        assert "requires_password_setup" not in body
        # 新規 salt (16B) が発行される
        assert len(b64decode(body["salt"])) == 16
        assert body["kdf_params"]["memory"] == 65536

    def test_unknown_user_dummy_salt_deterministic(self, client, db):
        a = _begin(client, "ghost").get_json()
        b = _begin(client, "ghost").get_json()
        assert a["migration_required"] is False
        # username に対し決定的 (列挙耐性)
        assert a["salt"] == b["salt"]
        assert len(b64decode(a["salt"])) == 16

    def test_unknown_vs_known_salt_differ(self, client, db, user):
        ghost = _begin(client, "ghost").get_json()
        known = _begin(client, "testuser").get_json()
        assert ghost["salt"] != known["salt"]

    def test_migrated_user_returns_stored_salt(self, client, db, user):
        # 先に移行を完了させる
        salt = _begin(client, "testuser").get_json()["salt"]
        _migrate(client, "testuser", _verifier(), salt)
        client.get("/logout")
        # 再度 begin すると保存済み salt + migration_required:false
        body = _begin(client, "testuser").get_json()
        assert body["migration_required"] is False
        assert body["salt"] == salt

    def test_passkey_only_requires_password_setup(self, client, db):
        u = User(username="pkonly", email="pk@example.com", user_type="personal")
        u.password_hash = None  # パスワード非保有
        db.session.add(u)
        db.session.commit()
        body = _begin(client, "pkonly").get_json()
        assert body["migration_required"] is True
        assert body["requires_password_setup"] is True

    def test_empty_username_400(self, client, db):
        resp = client.post("/auth/login/begin", json={"username": "  "})
        assert resp.status_code == 400


# ------------------------------------------------------------------
# /finish 移行パス
# ------------------------------------------------------------------

class TestLoginFinishMigrate:
    def test_migration_success(self, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        verifier = _verifier()
        resp = _migrate(client, "testuser", verifier, salt)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True and body["migrated"] is True
        # クライアントの rewrap ドライバが使う user_id / years を返す。
        # この user は temp-MK を持たないので rewrap 不要・years は空。
        assert body["user_id"] == User.query.filter_by(username="testuser").first().id
        assert body["needs_rewrap"] is False
        assert body["years"] == []

        migrated = User.query.filter_by(username="testuser").first()
        # 認証因子が確立されている
        assert bytes(migrated.login_salt) == b64decode(salt)
        assert bytes(migrated.login_server_hash) == _expected_hash(verifier)
        assert migrated.login_kdf_params["memory"] == 65536
        assert migrated.login_secret_version == 1
        # rewrap 未完なので password_hash はまだ残る
        assert migrated.password_hash is not None
        # passphrase wrapped_key が作られている
        wk = WrappedKey.query.filter_by(
            user_id=migrated.id, method=METHOD_PASSPHRASE,
        ).first()
        assert wk is not None
        assert bytes(wk.wrapped_master_key) == b"\x01" * 48
        assert bytes(wk.salt) == b64decode(salt)

    def test_migration_wrong_password_rejected(self, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        resp = _migrate(client, "testuser", _verifier(), salt, password="WRONG")
        assert resp.status_code == 401
        # 認証因子は確立されていない
        assert User.query.filter_by(username="testuser").first().login_salt is None

    def test_migration_salt_mismatch_rejected(self, client, db, user):
        _begin(client, "testuser")
        # begin で得たのとは別の salt を送る
        bad_salt = _b64(b"\x09" * 16)
        resp = _migrate(client, "testuser", _verifier(), bad_salt)
        assert resp.status_code == 400
        assert User.query.filter_by(username="testuser").first().login_salt is None

    def test_migration_invalid_wrap_iv_rejected(self, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        resp = _migrate(client, "testuser", _verifier(), salt, wrap_iv=_b64(b"\x02" * 8))
        assert resp.status_code == 400

    def test_migration_weak_kdf_rejected(self, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        resp = _migrate(
            client, "testuser", _verifier(), salt,
            login_kdf_params={"memory": 1024, "iterations": 1, "parallelism": 1},
        )
        assert resp.status_code == 400

    def test_migration_already_migrated_rejected(self, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        _migrate(client, "testuser", _verifier(), salt)
        client.get("/logout")
        # 2 回目の移行 finish は拒否 (login_salt が既に有る)
        salt2 = _begin(client, "testuser").get_json()["salt"]
        resp = _migrate(client, "testuser", _verifier(0xBB), salt2)
        assert resp.status_code == 400

    def test_migration_rejected_if_user_already_has_wrapped_key(self, client, db, user):
        # 旧ウィザードで MK を確立済み (wrapped_key 有 / login_salt 無) のユーザーは
        # 移行パスに通さない (新 MK 生成で既存鍵が孤立しデータ消失するのを防ぐ)。
        wk = WrappedKey(
            user_id=user.id, method=METHOD_PASSPHRASE,
            wrapped_master_key=b"\x01" * 48, wrap_iv=b"\x02" * 12,
            salt=b"\x03" * 16, kdf_params={"memory": 65536, "iterations": 3, "parallelism": 1},
        )
        db.session.add(wk)
        db.session.commit()
        salt = _begin(client, "testuser").get_json()["salt"]
        resp = _migrate(client, "testuser", _verifier(), salt)
        assert resp.status_code == 400
        assert User.query.filter_by(username="testuser").first().login_salt is None

    def test_migration_is_idempotent_retry_after_interrupt(self, client, db, user):
        # begin → (finish せず中断) → 再 begin → finish が成功する
        _begin(client, "testuser")  # 1 回目 (salt 破棄相当)
        salt2 = _begin(client, "testuser").get_json()["salt"]  # 2 回目で上書き
        resp = _migrate(client, "testuser", _verifier(), salt2)
        assert resp.status_code == 200


# ------------------------------------------------------------------
# /finish 通常パス
# ------------------------------------------------------------------

class TestLoginFinishNormal:
    def _setup_migrated(self, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        verifier = _verifier()
        _migrate(client, "testuser", verifier, salt)
        client.get("/logout")
        return salt, verifier

    def test_normal_login_success(self, client, db, user):
        _salt, verifier = self._setup_migrated(client, db, user)
        resp = client.post("/auth/login/finish", json={
            "username": "testuser",
            "login_verifier": _b64(verifier),
        })
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_normal_login_wrong_verifier_401(self, client, db, user):
        self._setup_migrated(client, db, user)
        resp = client.post("/auth/login/finish", json={
            "username": "testuser",
            "login_verifier": _b64(_verifier(0x00)),
        })
        assert resp.status_code == 401

    def test_normal_login_unmigrated_user_401(self, client, db, user):
        # 移行前ユーザーに通常パスでログイン試行 → 401 (login_salt 無)
        resp = client.post("/auth/login/finish", json={
            "username": "testuser",
            "login_verifier": _b64(_verifier()),
        })
        assert resp.status_code == 401

    def test_normal_login_unknown_user_401(self, client, db):
        resp = client.post("/auth/login/finish", json={
            "username": "ghost",
            "login_verifier": _b64(_verifier()),
        })
        assert resp.status_code == 401

    def test_finish_invalid_verifier_length_400(self, client, db, user):
        resp = client.post("/auth/login/finish", json={
            "username": "testuser",
            "login_verifier": _b64(b"\x01" * 16),  # 32B でない
        })
        assert resp.status_code == 400


class TestLoginPageRendering:
    """#385 PR-3a: LOGIN_SERVER_SECRET の有無で login_flow.mjs の読み込みを切替える。"""

    def test_login_flow_module_loaded_when_configured(self, app, client, db):
        # autouse fixture で LOGIN_SERVER_SECRET は設定済み。
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"js/auth/login_flow.mjs" in resp.data

    def test_login_flow_module_absent_when_unconfigured(self, app, client, db):
        app.config["LOGIN_SERVER_SECRET"] = ""
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"js/auth/login_flow.mjs" not in resp.data


def _change_pw(client, old_verifier, new_verifier, new_salt_b64, **overrides):
    payload = {
        "old_login_verifier": _b64(old_verifier),
        "login_verifier": _b64(new_verifier),
        "login_salt": new_salt_b64,
        "login_kdf_params": {"memory": 65536, "iterations": 3, "parallelism": 1},
        "wrapped_master_key": _b64(b"\x03" * 48),
        "wrap_iv": _b64(b"\x04" * 12),
    }
    payload.update(overrides)
    return client.post("/auth/login/change-password", json=payload)


class TestChangePassword:
    """#385 PR-4 §3.3: ログイン中のパスワード変更 (MK 不変・再 wrap)。"""

    def _migrate_and_login(self, client, user, salt_b64=None, verifier=None):
        salt = salt_b64 or _begin(client, "testuser").get_json()["salt"]
        v = verifier if verifier is not None else _verifier(0xAA)
        resp = _migrate(client, "testuser", v, salt)
        assert resp.status_code == 200  # finish 移行でログイン確立
        return salt, v

    def test_change_password_success(self, client, db, user):
        old_salt, old_v = self._migrate_and_login(client, user)
        new_salt = _b64(b"\x07" * 16)
        new_v = _verifier(0xCC)
        resp = _change_pw(client, old_v, new_v, new_salt)
        assert resp.status_code == 200, resp.get_json()
        u = User.query.filter_by(username="testuser").first()
        assert bytes(u.login_server_hash) == _expected_hash(new_v)
        assert bytes(u.login_salt) == b64decode(new_salt)
        wk = WrappedKey.query.filter_by(
            user_id=u.id, method=METHOD_PASSPHRASE,
        ).first()
        assert bytes(wk.wrapped_master_key) == b"\x03" * 48
        assert bytes(wk.salt) == b64decode(new_salt)

    def test_change_password_wrong_old_rejected(self, client, db, user):
        old_salt, old_v = self._migrate_and_login(client, user)
        resp = _change_pw(client, _verifier(0x00), _verifier(0xCC), _b64(b"\x07" * 16))
        assert resp.status_code == 401
        # 何も変わっていない (旧 verifier のハッシュのまま)
        u = User.query.filter_by(username="testuser").first()
        assert bytes(u.login_server_hash) == _expected_hash(old_v)

    def test_change_password_requires_auth(self, client, db, user):
        # 未ログインのクライアントは 401
        resp = _change_pw(client, _verifier(0xAA), _verifier(0xCC), _b64(b"\x07" * 16))
        assert resp.status_code == 401

    def test_change_password_invalid_material(self, client, db, user):
        old_salt, old_v = self._migrate_and_login(client, user)
        resp = _change_pw(client, old_v, _verifier(0xCC), _b64(b"\x07" * 16),
                          wrap_iv=_b64(b"\x04" * 8))  # 12B でない
        assert resp.status_code == 400

    def test_normal_login_after_change(self, client, db, user):
        old_salt, old_v = self._migrate_and_login(client, user)
        new_salt = _b64(b"\x07" * 16)
        new_v = _verifier(0xCC)
        assert _change_pw(client, old_v, new_v, new_salt).status_code == 200
        client.get("/logout")
        # 新 verifier で通常ログイン成功、旧 verifier は失敗
        ok = client.post("/auth/login/finish", json={
            "username": "testuser", "login_verifier": _b64(new_v)})
        assert ok.status_code == 200
        client.get("/logout")
        ng = client.post("/auth/login/finish", json={
            "username": "testuser", "login_verifier": _b64(old_v)})
        assert ng.status_code == 401


class TestChangePasswordPage:
    """#385 PR-4: /settings/password とインデックスのカードが login_derived_enabled
    (context processor inject_login_derived_flag 由来) で出し分けされる。"""

    def test_page_and_card_shown_when_configured(self, client, db, user):
        # 移行してログイン (LOGIN_SERVER_SECRET は autouse fixture で設定済)
        salt = _begin(client, "testuser").get_json()["salt"]
        _migrate(client, "testuser", _verifier(), salt)
        # /settings/ に「パスワード変更」カードが出る (context processor 経由)
        idx = client.get("/settings/")
        assert idx.status_code == 200
        assert "パスワード変更".encode() in idx.data
        # 変更ページも開ける
        page = client.get("/settings/password")
        assert page.status_code == 200
        assert b"change-password-form" in page.data

    def test_page_404_and_card_hidden_when_unconfigured(self, app, client, db, user):
        salt = _begin(client, "testuser").get_json()["salt"]
        _migrate(client, "testuser", _verifier(), salt)  # ログイン確立
        # LOGIN_SERVER_SECRET を外すと context processor が falsy → カード非表示・404
        app.config["LOGIN_SERVER_SECRET"] = ""
        idx = client.get("/settings/")
        assert idx.status_code == 200
        assert "パスワード変更".encode() not in idx.data
        assert client.get("/settings/password").status_code == 404
