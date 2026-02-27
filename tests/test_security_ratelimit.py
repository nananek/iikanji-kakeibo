"""レート制限テスト — Flask-Limiter による制限が機能することを検証"""

import pytest

from app.extensions import db as _db
from app.models.user import User
from app.models.account import AccountType


class TestLoginRateLimit:
    """POST /login — 10/minute"""

    def test_login_rate_limited_after_10_attempts(self, ratelimit_app, ratelimit_client):
        with ratelimit_app.app_context():
            u = User(username="rl_user", email="rl@test.com", user_type="personal")
            u.set_password("pass12345678")
            _db.session.add(u)
            _db.session.commit()

        for _ in range(10):
            resp = ratelimit_client.post("/login", data={
                "username": "rl_user",
                "password": "wrongpass",
            })
            assert resp.status_code == 200

        # 11回目 → 429
        resp = ratelimit_client.post("/login", data={
            "username": "rl_user",
            "password": "wrongpass",
        })
        assert resp.status_code == 429


class TestAuditorLoginRateLimit:
    """POST /login/auditor — 10/minute"""

    def test_auditor_login_rate_limited(self, ratelimit_app, ratelimit_client):
        for _ in range(10):
            ratelimit_client.post("/login/auditor", data={
                "username": "nobody",
                "password": "nopass",
            })

        resp = ratelimit_client.post("/login/auditor", data={
            "username": "nobody",
            "password": "nopass",
        })
        assert resp.status_code == 429


class TestRegisterRateLimit:
    """POST /register — 5/minute"""

    def test_register_rate_limited_after_5_attempts(self, ratelimit_app, ratelimit_client):
        # seed_accounts_for_user が account_types を必要とするため投入
        from app.models.account import AccountType
        with ratelimit_app.app_context():
            types = [
                AccountType(name="資産", code="asset", normal_balance="debit", display_order=1),
                AccountType(name="負債", code="liability", normal_balance="credit", display_order=2),
                AccountType(name="純資産", code="equity", normal_balance="credit", display_order=3),
                AccountType(name="収益", code="revenue", normal_balance="credit", display_order=4),
                AccountType(name="費用", code="expense", normal_balance="debit", display_order=5),
            ]
            _db.session.add_all(types)
            _db.session.commit()

        for i in range(5):
            ratelimit_client.post("/register", data={
                "username": f"rluser{i}",
                "email": f"rl{i}@test.com",
                "password": "12345678",
                "password_confirm": "12345678",
            })

        # 6回目 → 429
        resp = ratelimit_client.post("/register", data={
            "username": "rluser99",
            "email": "rl99@test.com",
            "password": "12345678",
            "password_confirm": "12345678",
        })
        assert resp.status_code == 429
