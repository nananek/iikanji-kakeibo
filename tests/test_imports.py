"""CSV / OFX / Web 取込ビューのテスト

ファイル/テキスト → 解析 → confirm → 一括取込のフローを mock 経由で検証。
"""

import io
import json
from datetime import date
from unittest.mock import patch

from app.models.journal import JournalEntry


# --- CSV Import ---


class TestCsvImportUpload:
    def test_unauthenticated(self, client):
        resp = client.get("/csv-import/")
        assert resp.status_code in (302, 401)

    def test_get_renders_form(self, logged_in_client, accounts):
        resp = logged_in_client.get("/csv-import/")
        assert resp.status_code == 200

    def test_post_no_file(self, logged_in_client, accounts):
        resp = logged_in_client.post("/csv-import/", data={
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200  # form 再表示

    def test_post_no_payment_account(self, logged_in_client, accounts):
        csv_bytes = b"date,description,amount\n2026-02-15,test,1000"
        resp = logged_in_client.post("/csv-import/", data={
            "csv_file": (io.BytesIO(csv_bytes), "test.csv"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200

    def test_post_oversized_file(self, logged_in_client, accounts):
        big = b"x" * (6 * 1024 * 1024)  # 6MB
        resp = logged_in_client.post("/csv-import/", data={
            "csv_file": (io.BytesIO(big), "big.csv"),
            "payment_account_code": "1010",
        }, content_type="multipart/form-data")
        assert resp.status_code == 200

    def test_post_unparseable_csv(self, logged_in_client, accounts):
        resp = logged_in_client.post("/csv-import/", data={
            "csv_file": (io.BytesIO(b""), "empty.csv"),
            "payment_account_code": "1010",
        }, content_type="multipart/form-data")
        assert resp.status_code == 200

    def test_post_valid_csv_redirects_to_mapping(self, logged_in_client, accounts):
        csv_bytes = (
            "日付,摘要,金額\n"
            "2026-02-15,スーパー,1000\n"
            "2026-02-16,コンビニ,500\n"
        ).encode("utf-8")
        resp = logged_in_client.post("/csv-import/", data={
            "csv_file": (io.BytesIO(csv_bytes), "test.csv"),
            "payment_account_code": "1010",
        }, content_type="multipart/form-data")
        assert resp.status_code in (302, 303)
        assert "/csv-import/mapping" in resp.headers["Location"]


class TestCsvImportMapping:
    def test_no_data_redirects_to_upload(self, logged_in_client, accounts):
        resp = logged_in_client.get("/csv-import/mapping")
        assert resp.status_code in (302, 303)
        assert "/csv-import/" in resp.headers["Location"]

    def test_get_after_upload(self, logged_in_client, accounts):
        csv_bytes = (
            "日付,摘要,出金,入金\n"
            "2026-02-15,スーパー,1000,0\n"
        ).encode("utf-8")
        # Upload first
        logged_in_client.post("/csv-import/", data={
            "csv_file": (io.BytesIO(csv_bytes), "test.csv"),
            "payment_account_code": "1010",
        }, content_type="multipart/form-data")
        # Now get mapping page
        resp = logged_in_client.get("/csv-import/mapping")
        assert resp.status_code == 200


# --- OFX Import ---


_OFX_SAMPLE = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>JPY
<BANKACCTFROM>
<BANKID>0001
<ACCTID>1234567
<ACCTTYPE>SAVINGS
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260201
<DTEND>20260228
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260215
<TRNAMT>-1500
<FITID>TX001
<NAME>SUPERMARKET
</STMTTRN>
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260225
<TRNAMT>250000
<FITID>TX002
<NAME>SALARY
</STMTTRN>
</BANKTRANLIST>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>"""


class TestOfxImportUpload:
    def test_unauthenticated(self, client):
        resp = client.get("/ofx-import/")
        assert resp.status_code in (302, 401)

    def test_get_renders_form(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ofx-import/")
        assert resp.status_code == 200

    def test_post_no_file(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ofx-import/", data={
            "payment_account_code": "1010",
        })
        assert resp.status_code == 200

    def test_post_no_account(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ofx-import/", data={
            "ofx_file": (io.BytesIO(_OFX_SAMPLE), "test.ofx"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200

    def test_post_invalid_ofx(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ofx-import/", data={
            "ofx_file": (io.BytesIO(b"not-ofx"), "test.ofx"),
            "payment_account_code": "1010",
        }, content_type="multipart/form-data")
        # parse 失敗または 0件 → 200 で再表示
        assert resp.status_code == 200

    def test_post_valid_ofx_redirects(self, logged_in_client, accounts):
        with patch("app.views.ofx_import.parse_ofx") as mock_parse:
            mock_parse.return_value = {
                "account_id": "1234567",
                "rows": [
                    {"date": "2026-02-15", "description": "X",
                     "deposit": 0, "withdrawal": 1500},
                ],
            }
            resp = logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(_OFX_SAMPLE), "test.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")
        assert resp.status_code in (302, 303)
        assert "/ofx-import/confirm" in resp.headers["Location"]


class TestOfxImportConfirm:
    def test_no_data_redirects(self, logged_in_client, accounts):
        resp = logged_in_client.get("/ofx-import/confirm")
        assert resp.status_code in (302, 303)
        assert "/ofx-import" in resp.headers["Location"]

    def test_full_flow_imports_entries(self, db, logged_in_client, user, accounts):
        # mock parse_ofx → upload → confirm
        with patch("app.views.ofx_import.parse_ofx") as mock_parse:
            mock_parse.return_value = {
                "account_id": "X",
                "rows": [
                    {"date": "2026-02-15", "description": "支払",
                     "deposit": 0, "withdrawal": 1500},
                    {"date": "2026-02-25", "description": "給与",
                     "deposit": 250000, "withdrawal": 0},
                ],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(_OFX_SAMPLE), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")

        # confirm GET
        resp = logged_in_client.get("/ofx-import/confirm")
        assert resp.status_code == 200

        # confirm POST: 取込実行
        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "支払",
             "deposit": 0, "withdrawal": 1500, "category_code": "5010"},
            {"enabled": True, "date": "2026-02-25", "description": "給与",
             "deposit": 250000, "withdrawal": 0, "category_code": "4010"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        # 2件取り込み
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 2

    def test_disabled_rows_skipped(self, db, logged_in_client, user, accounts):
        with patch("app.views.ofx_import.parse_ofx") as mock_parse:
            mock_parse.return_value = {
                "account_id": "X",
                "rows": [{"date": "2026-02-15", "description": "x",
                          "deposit": 0, "withdrawal": 100}],
            }
            logged_in_client.post("/ofx-import/", data={
                "ofx_file": (io.BytesIO(b"x"), "x.ofx"),
                "payment_account_code": "1010",
            }, content_type="multipart/form-data")

        rows = [
            {"enabled": False, "date": "2026-02-15",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/ofx-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="ofx"
        ).count() == 0

    def test_post_no_data_redirects(self, logged_in_client, accounts):
        resp = logged_in_client.post("/ofx-import/confirm", data={})
        assert resp.status_code in (302, 303)


# --- Web Import ---


def _setup_web_import_session(logged_in_client, parsed_rows,
                               payment_code="1010"):
    """POST /web-import/ が無効化されたため、parsed データを
    save_import_data + session 直接設定で投入するヘルパー (confirm フロー
    のテスト用)。"""
    from app.views.helpers import save_import_data
    key = save_import_data(parsed_rows)
    with logged_in_client.session_transaction() as sess:
        sess["web_data_key"] = key
        sess["web_payment_account_code"] = payment_code
    return key


def _make_e2ee_ai_config(db, user_id):
    """E2EE モード用 AI 設定をセットアップ。"""
    from app.models.ai_config import UserAIConfig
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_blob=b"\xAA" * 48, api_key_iv=b"\xBB" * 12,
        model_name="gpt-4o-mini",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _make_non_e2ee_ai_config(db, user_id):
    """blob/iv 未保存 (is_e2ee=False) の AI 設定。Fernet 廃止後は
    旧 Fernet ユーザーや手動 DB 操作で発生し得る状態。"""
    from app.models.ai_config import UserAIConfig
    cfg = UserAIConfig(
        user_id=user_id, provider="openai",
        api_key_blob=None, api_key_iv=None,
        model_name="gpt-4o-mini",
    )
    db.session.add(cfg)
    db.session.commit()
    return cfg


class TestWebImportUpload:
    """クライアント完結 E2EE モード対応後の GET/POST 挙動。"""

    def test_unauthenticated(self, client):
        resp = client.get("/web-import/")
        assert resp.status_code in (302, 401)

    def test_get_no_config_shows_registration_warning(
        self, db, logged_in_client, user, accounts,
    ):
        resp = logged_in_client.get("/web-import/")
        assert resp.status_code == 200
        assert "外部AI設定が登録されていません" in resp.data.decode()

    def test_get_non_e2ee_shows_migration_required_banner(
        self, db, logged_in_client, user, accounts,
    ):
        """blob/iv 未保存 → 「E2EE モードに移行」warning + フォーム disabled。"""
        _make_non_e2ee_ai_config(db, user.id)
        resp = logged_in_client.get("/web-import/")
        html = resp.data.decode()
        assert "クライアント完結の E2EE モードに移行しました" in html
        assert "E2EE 形式で再登録" in html
        assert "E2EE モードで抽出します" not in html

    def test_get_e2ee_shows_success_banner_and_enabled_form(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.get("/web-import/")
        html = resp.data.decode()
        assert "E2EE モードで抽出します" in html
        assert "ブラウザから LLM に直接送信" in html
        assert "e2eeFullClientMode = true" in html

    def test_post_json_saves_session_and_returns_redirect(
        self, db, logged_in_client, user, accounts,
    ):
        """新 JSON フロー: parsed_transactions を受けて session 保存 + URL 返却。"""
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [
                    {"date": "2026-02-15", "description": "ATM",
                     "deposit": 0, "withdrawal": 5000},
                ],
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "/web-import/confirm" in body["redirect_url"]
        # 後続 GET で confirm に進めることを確認
        confirm_resp = logged_in_client.get("/web-import/confirm")
        assert confirm_resp.status_code == 200

    def test_post_json_rejects_empty(self, db, logged_in_client, user, accounts):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={"parsed_transactions": [], "payment_account_code": "1010"},
        )
        assert resp.status_code == 400
        assert "parsed_transactions" in resp.get_json()["error"]

    def test_post_json_rejects_too_many_rows(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{"date": "2026-02-15"}] * 1001,
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 400
        assert "行数" in resp.get_json()["error"]

    def test_post_json_rejects_missing_payment_account(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{"date": "2026-02-15"}],
            },
        )
        assert resp.status_code == 400

    def test_post_json_rejects_invalid_payment_account(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{"date": "2026-02-15"}],
                "payment_account_code": "9999",
            },
        )
        assert resp.status_code == 400
        assert "口座" in resp.get_json()["error"]

    def test_post_non_json_returns_400(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post("/web-import/", data={"raw_text": "x"})
        assert resp.status_code == 400
        assert "JSON" in resp.get_json()["error"]

    def test_post_rejects_non_e2ee_config_returns_403(
        self, db, logged_in_client, user, accounts,
    ):
        """blob/iv 未保存 (is_e2ee=False) のユーザーが直接 POST しても 403。
        UI でフォーム無効化される前提だが、サーバ側でも防御。"""
        _make_non_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{"date": "2026-02-15"}],
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 403
        assert "E2EE" in resp.get_json()["error"]

    def test_post_rejects_no_config_returns_403(
        self, db, logged_in_client, user, accounts,
    ):
        """AI 設定なしのユーザーが直接 POST しても 403。"""
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{"date": "2026-02-15"}],
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 403

    def test_post_validates_long_description(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{
                    "date": "2026-02-15", "description": "x" * 501,
                    "deposit": 0, "withdrawal": 100,
                }],
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 400
        assert "description" in resp.get_json()["error"]

    def test_post_validates_amount_range(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": [{
                    "date": "2026-02-15", "description": "x",
                    "deposit": -1, "withdrawal": 0,
                }],
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 400
        assert "deposit" in resp.get_json()["error"]

    def test_post_validates_non_dict_row(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            json={
                "parsed_transactions": ["not a dict"],
                "payment_account_code": "1010",
            },
        )
        assert resp.status_code == 400

    def test_post_validates_nan_amount(
        self, db, logged_in_client, user, accounts,
    ):
        """json.loads は NaN トークンを許容するため、validator が
        math.isfinite で弾かないと後段の int(nan) が 500 エラーになる。"""
        _make_e2ee_ai_config(db, user.id)
        # Flask test client は json= で送ると標準 json.dumps を使うので
        # NaN を直接埋め込めない → raw body 送信
        resp = logged_in_client.post(
            "/web-import/",
            data=(
                '{"parsed_transactions": [{"date": "2026-02-15", '
                '"deposit": NaN, "withdrawal": 0}], '
                '"payment_account_code": "1010"}'
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "deposit" in resp.get_json()["error"]

    def test_post_validates_inf_amount(
        self, db, logged_in_client, user, accounts,
    ):
        _make_e2ee_ai_config(db, user.id)
        resp = logged_in_client.post(
            "/web-import/",
            data=(
                '{"parsed_transactions": [{"date": "2026-02-15", '
                '"withdrawal": Infinity, "deposit": 0}], '
                '"payment_account_code": "1010"}'
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "withdrawal" in resp.get_json()["error"]


class TestWebImportConfirm:
    def test_no_data_redirects(self, logged_in_client, accounts):
        resp = logged_in_client.get("/web-import/confirm")
        assert resp.status_code in (302, 303)

    def test_full_flow(self, db, logged_in_client, user, accounts):
        # parse_web_text 経由ではなく session を直接仕込んで
        # confirm フロー (仕訳生成) だけをテスト。
        _setup_web_import_session(logged_in_client, [
            {"date": "2026-02-15", "description": "セブン",
             "deposit": 0, "withdrawal": 500},
        ])

        resp = logged_in_client.get("/web-import/confirm")
        assert resp.status_code == 200

        rows = [
            {"enabled": True, "date": "2026-02-15", "description": "セブン",
             "deposit": 0, "withdrawal": 500, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web"
        ).count() == 1

    def test_post_no_rows(self, logged_in_client, accounts):
        resp = logged_in_client.post("/web-import/confirm", data={})
        assert resp.status_code in (302, 303)

    def test_confirm_skips_disabled_and_invalid_rows(
        self, db, logged_in_client, user, accounts,
    ):
        """enabled=false / 日付欠落 / カテゴリ欠落 / 金額 0 は skip される。"""
        _setup_web_import_session(
            logged_in_client,
            [{"date": "2026-02-15", "description": "x"}],
        )
        rows = [
            # enabled=False は skip
            {"enabled": False, "date": "2026-02-15", "description": "off",
             "deposit": 0, "withdrawal": 100, "category_code": "5010"},
            # 日付欠落は skip
            {"enabled": True, "date": "", "description": "no-date",
             "deposit": 100, "withdrawal": 0, "category_code": "5010"},
            # category_code 欠落は skip
            {"enabled": True, "date": "2026-02-15", "description": "no-cat",
             "deposit": 100, "withdrawal": 0, "category_code": ""},
            # 金額両方 0 は skip
            {"enabled": True, "date": "2026-02-15", "description": "zero",
             "deposit": 0, "withdrawal": 0, "category_code": "5010"},
            # 正常 — import される
            {"enabled": True, "date": "2026-02-15", "description": "ok",
             "deposit": 0, "withdrawal": 200, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        # 5 件中 1 件のみ取込
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web",
        ).count() == 1

    def test_confirm_skips_closed_period(
        self, db, logged_in_client, user, accounts,
    ):
        """確定済み期間に該当する行は skip される。"""
        from app.models.fiscal import FiscalClose
        db.session.add(FiscalClose(user_id=user.id, year=2026, closed_period=2))
        db.session.commit()
        _setup_web_import_session(
            logged_in_client, [{"date": "2026-02-15"}],
        )
        rows = [
            # 2026-02 は確定済み → skip
            {"enabled": True, "date": "2026-02-15", "description": "blocked",
             "deposit": 100, "withdrawal": 0, "category_code": "5010"},
            # 2026-03 は未確定 → import
            {"enabled": True, "date": "2026-03-15", "description": "ok",
             "deposit": 200, "withdrawal": 0, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web",
        ).count() == 1

    def test_confirm_skips_locked_account_code(
        self, db, logged_in_client, user, accounts,
    ):
        """提出済みロック科目を含む行は skip される。"""
        from app.models.tax_form import TaxFormField, TaxFormMapping
        # 提出済みフラグを立てる科目を作る
        field = TaxFormField.query.first()
        if field is None:
            import pytest
            pytest.skip("tax_form 未シード")
        m = TaxFormMapping(
            user_id=user.id, account_code="5010",
            tax_form_field_id=field.id, year=2026, submitted=True,
        )
        db.session.add(m)
        db.session.commit()
        _setup_web_import_session(
            logged_in_client, [{"date": "2026-02-15"}],
        )
        rows = [
            # 5010 はロック → skip
            {"enabled": True, "date": "2026-02-15", "description": "locked",
             "deposit": 100, "withdrawal": 0, "category_code": "5010"},
        ]
        resp = logged_in_client.post("/web-import/confirm", data={
            "import_rows": json.dumps(rows),
            "old_year_action": "skip",
        })
        assert resp.status_code in (302, 303)
        assert JournalEntry.query.filter_by(
            user_id=user.id, source="web",
        ).count() == 0
