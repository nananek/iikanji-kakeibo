import pytest
from datetime import date, datetime, timezone

from app import create_app
from app.config import Config
from app.extensions import db as _db
from app.models.user import User
from app.models.account import AccountType, Account
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.fiscal import FiscalClose
from app.models.api_key import APIKey
from app.models.voucher import Voucher


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    STORAGE_BACKEND = "local"
    STORAGE_LOCAL_DIR = "/tmp/iikanji-test-vouchers"
    # llama.cpp はテスト時に常時有効状態にして、設定保存系テストを通す
    LLAMA_CPP_URL = "http://test-llama-cpp:8080"
    # 規約同意管理はデフォルトでは無効化 (既存テストへの影響を避ける)。
    # 同意フローを検証するテストは個別に monkeypatch で値を設定する。
    CURRENT_TERMS_VERSION = ""


class CsrfTestConfig(Config):
    """CSRF 有効 — CSRF検証テスト用"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = True
    RATELIMIT_ENABLED = False


class RateLimitTestConfig(Config):
    """レート制限有効 — レート制限テスト用"""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = True


@pytest.fixture(scope="session")
def app():
    app = create_app(TestConfig)
    with app.app_context():
        from sqlalchemy import event

        @event.listens_for(_db.engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        _db.create_all()
        yield app
        _db.drop_all()


@pytest.fixture(autouse=True)
def _clean_tables(app):
    """各テスト後にすべてのテーブルをクリーンアップ"""
    yield
    with app.app_context():
        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


@pytest.fixture
def db(app):
    with app.app_context():
        yield _db


@pytest.fixture
def client(app):
    return app.test_client()


# --- ユーザー ---


@pytest.fixture
def user(db):
    u = User(
        username="testuser",
        email="test@example.com",
        user_type="personal",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def auditor(db):
    u = User(
        username="auditor",
        email="auditor@example.com",
        user_type="auditor",
    )
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


# --- 科目区分・科目 ---


@pytest.fixture
def account_types(db):
    types = [
        AccountType(name="資産", code="asset", normal_balance="debit", display_order=1),
        AccountType(name="負債", code="liability", normal_balance="credit", display_order=2),
        AccountType(name="純資産", code="equity", normal_balance="credit", display_order=3),
        AccountType(name="収益", code="revenue", normal_balance="credit", display_order=4),
        AccountType(name="費用", code="expense", normal_balance="debit", display_order=5),
    ]
    db.session.add_all(types)
    db.session.commit()
    return {t.code: t for t in types}


@pytest.fixture
def accounts(db, user, account_types):
    """テスト用の基本科目セット"""
    accts = [
        Account(user_id=user.id, account_type_id=account_types["asset"].id,
                code="1010", name="現金", is_active=True, display_order=10),
        Account(user_id=user.id, account_type_id=account_types["asset"].id,
                code="1020", name="普通預金", is_active=True, display_order=20),
        Account(user_id=user.id, account_type_id=account_types["liability"].id,
                code="2010", name="クレジットカード", is_active=True, display_order=30),
        Account(user_id=user.id, account_type_id=account_types["equity"].id,
                code="3010", name="元入金", system_role="capital",
                is_active=True, display_order=40),
        Account(user_id=user.id, account_type_id=account_types["equity"].id,
                code="3020", name="繰越利益", system_role="retained_earnings",
                is_active=True, display_order=50),
        Account(user_id=user.id, account_type_id=account_types["equity"].id,
                code="3030", name="事業主", system_role="proprietor",
                is_active=True, display_order=60),
        Account(user_id=user.id, account_type_id=account_types["revenue"].id,
                code="4010", name="給与収入", is_active=True, display_order=70),
        Account(user_id=user.id, account_type_id=account_types["expense"].id,
                code="5010", name="食費", is_active=True, display_order=80),
        Account(user_id=user.id, account_type_id=account_types["expense"].id,
                code="5020", name="住居費", is_active=True, display_order=90),
    ]
    db.session.add_all(accts)
    db.session.commit()
    return {a.code: a for a in accts}


# --- API キー ---


@pytest.fixture
def api_key_raw(db, user):
    """(raw_key, APIKey) のタプルを返す"""
    raw_key, key_hash, key_prefix = APIKey.generate()
    key = APIKey(
        user_id=user.id,
        name="テスト用キー",
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes="journals:create,journals:read,journals:delete,ai:analyze,reports:read,backup:restore",
        is_active=True,
    )
    db.session.add(key)
    db.session.commit()
    return raw_key, key


def _auth_header(raw_key):
    return {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture
def auth_header(api_key_raw):
    raw_key, _ = api_key_raw
    return _auth_header(raw_key)


@pytest.fixture
def backup_skeleton(user):
    """空ユーザ用の最小 backup dict (BU-4b restore テスト用ベース)。"""
    return {
        "version": "1.0",
        "exported_at": "2026-01-01T00:00:00+00:00",
        "user_id": user.id,
        "data": {
            "accounts": [],
            "fiscal_closes": [],
            "journal_entries": [],
            "journal_entry_lines": [],
            "medical_expenses": [],
            "balance_cache_blobs": [],
            "vouchers": [],
            "ai_drafts": [],
            "user_ai_config": None,
            "webhook_configs": [],
            "tax_form_mappings": [],
            "csv_column_profiles": [],
        },
    }


# --- ヘルパー ---


@pytest.fixture
def logged_in_client(app, client, user):
    """ログイン済みのテストクライアント"""
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


@pytest.fixture
def passkey_only_user(db, user):
    """パスキー専用モード ON + パスキー 1 本 + 有効リカバリコード。

    生成したリカバリコードの生文字列は `user._test_recovery_code_raw` 属性に
    保存される。リカバリログインテストで参照可能。
    """
    from app.models.webauthn import WebAuthnCredential
    user.passkey_only_login = True
    raw = user.set_recovery_code()
    user._test_recovery_code_raw = raw
    cred = WebAuthnCredential(
        user_id=user.id,
        credential_id=b"test-credential-id-bytes",
        credential_public_key=b"test-public-key-bytes",
        current_sign_count=0,
        name="テスト用パスキー",
    )
    db.session.add(cred)
    db.session.commit()
    return user


@pytest.fixture
def second_user(db):
    """IDOR テスト用の別ユーザー"""
    u = User(
        username="otheruser",
        email="other@example.com",
        user_type="personal",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    u.set_password("password123")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def second_user_accounts(db, second_user, account_types):
    """second_user に属する科目セット"""
    accts = [
        Account(user_id=second_user.id, account_type_id=account_types["asset"].id,
                code="1010", name="現金", is_active=True, display_order=10),
        Account(user_id=second_user.id, account_type_id=account_types["expense"].id,
                code="5010", name="食費", is_active=True, display_order=80),
    ]
    db.session.add_all(accts)
    db.session.commit()
    return {a.code: a for a in accts}


# --- CSRF / RateLimit テスト用 app ---


@pytest.fixture
def csrf_app():
    app = create_app(CsrfTestConfig)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()
        _db.drop_all()


@pytest.fixture
def csrf_client(csrf_app):
    return csrf_app.test_client()


@pytest.fixture
def ratelimit_app():
    app = create_app(RateLimitTestConfig)
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.rollback()
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()
        _db.drop_all()


@pytest.fixture
def ratelimit_client(ratelimit_app):
    return ratelimit_app.test_client()


# --- ヘルパー ---


def make_journal(db, user_id, acct_debit_code, acct_credit_code, amount,
                 entry_date=None, description="テスト仕訳", source="journal",
                 fiscal_period=None, entry_number=None):
    """テスト用の仕訳を作成するヘルパー

    E3-F PR-D-6-5 (055): 平文列 date / description / source / fiscal_period は
    DROP 済のため JournalEntry には書き込まない。entry_date / source /
    fiscal_period 引数は fiscal_year / fiscal_month / is_closing の平文メタ列を
    導出するためにのみ使用する (本番の create_journal_entry / closing 生成と同じ
    規約)。fiscal_month は fiscal_period 明示時はそれ、未指定なら entry_date.month。
    source == "closing" は is_closing=True / fiscal_month=16。
    """
    if entry_date is None:
        entry_date = date(2026, 1, 15)
    if entry_number is None:
        from app.services.accounting import get_next_entry_number
        entry_number = get_next_entry_number(user_id)
    is_closing = (source == "closing")
    if fiscal_period is not None:
        fiscal_month = fiscal_period
    elif is_closing:
        fiscal_month = 16
    else:
        fiscal_month = entry_date.month
    entry = JournalEntry(
        user_id=user_id,
        entry_number=entry_number,
        fiscal_year=entry_date.year,  # E3 後の年度フィルタ用
        is_closing=is_closing,
        fiscal_month=fiscal_month,
    )
    entry.lines = [
        JournalEntryLine(account_user_id=user_id,
                         account_code=acct_debit_code,
                         debit_amount=amount, credit_amount=0),
        JournalEntryLine(account_user_id=user_id,
                         account_code=acct_credit_code,
                         debit_amount=0, credit_amount=amount),
    ]
    db.session.add(entry)
    db.session.commit()
    return entry


def _dummy_blob_b64(n_bytes=48, fill=0x42):
    """E3-F PR-C 以降、POST API は encrypted_blob/blob_iv を必須化したため、
    テストでダミー base64 payload を生成するヘルパー。
    """
    from base64 import b64encode
    return b64encode(bytes([fill]) * n_bytes).decode("ascii")


def encrypted_payload(n_blob_bytes=48, fill=0x42):
    """テスト用に encrypted_blob/blob_iv の dict を返す。POST payload に **展開
    して使う:
        client.post(url, json={**encrypted_payload(), ...其他フィールド})
    """
    return {
        "encrypted_blob": _dummy_blob_b64(n_blob_bytes, fill),
        "blob_iv": _dummy_blob_b64(12, fill),
    }


def encrypt_line(line_dict, n_blob_bytes=48, fill=0x42):
    """既存の line dict にダミー暗号化 payload を merge して返す。"""
    return {**line_dict, **encrypted_payload(n_blob_bytes, fill)}


def encrypt_lines(line_dicts, n_blob_bytes=48, fill=0x42):
    """line dict のリストすべてに暗号化 payload を付与する。"""
    return [encrypt_line(ld, n_blob_bytes, fill) for ld in line_dicts]


def make_voucher(db, user_id, journal_entry_id=None,
                 image_key="vouchers/1/1.jpg", image_mime="image/jpeg"):
    """テスト用の証憑を作成するヘルパー。

    E5 PR-5 (#111): vouchers.image_mime 列は DROP 済。後方互換のため
    image_mime 引数は受け取るが無視する (既存呼び出し側の変更を不要にする)。
    """
    v = Voucher(
        user_id=user_id,
        journal_entry_id=journal_entry_id,
        image_key=image_key,
    )
    db.session.add(v)
    db.session.commit()
    return v
