"""csv_import サービスのテスト"""

from datetime import date

import pytest

from app.services.csv_import import (
    detect_encoding,
    parse_amount,
    parse_csv_full,
    parse_csv_preview,
    parse_date,
    save_column_profile,
    load_column_profile,
    validate_ai_column_mapping,
)


# ============================================================
# detect_encoding
# ============================================================

class TestDetectEncoding:
    def test_utf8(self):
        # utf-8-sig は BOM なし UTF-8 も受け入れるので先にマッチする
        enc = detect_encoding("日本語".encode("utf-8"))
        assert enc in ("utf-8-sig", "utf-8")

    def test_utf8_bom(self):
        assert detect_encoding("日本語".encode("utf-8-sig")) == "utf-8-sig"

    def test_cp932(self):
        raw = "日本語テスト".encode("cp932")
        assert detect_encoding(raw) in ("cp932", "shift_jis")

    def test_shift_jis(self):
        # Shift-JIS 固有文字（CP932 と同じだが明示的にテスト）
        raw = "あいうえお".encode("shift_jis")
        enc = detect_encoding(raw)
        assert raw.decode(enc) == "あいうえお"

    def test_euc_jp(self):
        # EUC-JP 固有のバイト列 — 短い文字列は UTF-8/CP932 で先にデコード
        # されることがあるため、十分に長い文字列で検証
        text = "日本語のエンコーディング判定をテストする文章です。表示確認用。"
        raw = text.encode("euc-jp")
        enc = detect_encoding(raw)
        assert raw.decode(enc) == text

    def test_ascii(self):
        assert detect_encoding(b"hello,world") == "utf-8-sig"

    def test_empty(self):
        enc = detect_encoding(b"")
        assert enc == "utf-8-sig"


# ============================================================
# parse_amount
# ============================================================

class TestParseAmount:
    def test_plain_integer(self):
        assert parse_amount("1234") == 1234

    def test_comma_separated(self):
        assert parse_amount("1,234,567") == 1234567

    def test_yen_sign_fullwidth(self):
        assert parse_amount("￥1,234") == 1234

    def test_yen_sign_halfwidth(self):
        assert parse_amount("¥5,000") == 5000

    def test_yen_sign_unicode(self):
        assert parse_amount("\u00a51,000") == 1000

    def test_yen_suffix(self):
        assert parse_amount("1234円") == 1234

    def test_negative_preserved(self):
        """マイナス値は符号を保持する（反転は呼び出し側で処理）"""
        assert parse_amount("-500") == -500

    def test_float_truncated(self):
        assert parse_amount("1234.56") == 1234

    def test_empty_string(self):
        assert parse_amount("") == 0

    def test_none(self):
        assert parse_amount(None) == 0

    def test_whitespace_only(self):
        assert parse_amount("   ") == 0

    def test_dash_only(self):
        assert parse_amount("-") == 0

    def test_non_numeric(self):
        assert parse_amount("abc") == 0

    def test_padded_whitespace(self):
        assert parse_amount("  1,000  ") == 1000


# ============================================================
# parse_date
# ============================================================

class TestParseDate:
    def test_yyyy_slash(self):
        assert parse_date("2026/01/15", "%Y/%m/%d") == date(2026, 1, 15)

    def test_yyyy_hyphen(self):
        assert parse_date("2026-01-15", "%Y-%m-%d") == date(2026, 1, 15)

    def test_japanese_format(self):
        assert parse_date("2026年1月15日", "%Y年%m月%d日") == date(2026, 1, 15)

    def test_yy_slash(self):
        assert parse_date("26/01/15", "%y/%m/%d") == date(2026, 1, 15)

    def test_mm_dd_yyyy(self):
        assert parse_date("01/15/2026", "%m/%d/%Y") == date(2026, 1, 15)

    def test_fallback_auto_detect(self):
        """指定フォーマットが合わなくてもフォールバックで解析"""
        result = parse_date("2026/03/20", "%Y-%m-%d")  # 不一致フォーマット
        assert result == date(2026, 3, 20)

    def test_empty_string(self):
        assert parse_date("", "%Y/%m/%d") is None

    def test_none(self):
        assert parse_date(None, "%Y/%m/%d") is None

    def test_whitespace_only(self):
        assert parse_date("   ", "%Y/%m/%d") is None

    def test_invalid_date(self):
        assert parse_date("not-a-date", "%Y/%m/%d") is None

    def test_whitespace_trimmed(self):
        assert parse_date("  2026/01/15  ", "%Y/%m/%d") == date(2026, 1, 15)


# ============================================================
# parse_csv_preview
# ============================================================

class TestParseCsvPreview:
    def _make_csv(self, text, encoding="utf-8"):
        return text.encode(encoding)

    def test_basic(self):
        raw = self._make_csv("日付,摘要,入金,出金\n2026/01/01,給料,300000,\n2026/01/05,食費,,5000\n")
        result = parse_csv_preview(raw)
        assert result["headers"] == ["日付", "摘要", "入金", "出金"]
        assert result["total_rows"] == 2
        assert len(result["rows"]) == 2
        assert result["rows"][0] == ["2026/01/01", "給料", "300000", ""]

    def test_max_rows(self):
        lines = ["日付,金額"] + [f"2026/01/{i:02d},{i*100}" for i in range(1, 31)]
        raw = self._make_csv("\n".join(lines))
        result = parse_csv_preview(raw, max_rows=5)
        assert len(result["rows"]) == 5
        assert result["total_rows"] == 30

    def test_empty_csv(self):
        result = parse_csv_preview(b"")
        assert result["headers"] == []
        assert result["rows"] == []
        assert result["total_rows"] == 0

    def test_header_only(self):
        raw = self._make_csv("日付,摘要,金額\n")
        result = parse_csv_preview(raw)
        assert result["headers"] == ["日付", "摘要", "金額"]
        assert result["rows"] == []
        assert result["total_rows"] == 0

    def test_blank_rows_skipped(self):
        raw = self._make_csv("H1,H2\n\n  ,  \nA,B\n")
        result = parse_csv_preview(raw)
        assert result["total_rows"] == 1
        assert result["rows"][0] == ["A", "B"]

    def test_utf8_bom(self):
        raw = "\ufeff日付,金額\n2026/01/01,100\n".encode("utf-8-sig")
        result = parse_csv_preview(raw)
        assert result["headers"] == ["日付", "金額"]

    def test_cp932_encoding(self):
        raw = "日付,金額\n2026/01/01,100\n".encode("cp932")
        result = parse_csv_preview(raw)
        assert result["encoding"] in ("cp932", "shift_jis")
        assert result["headers"] == ["日付", "金額"]
        assert result["total_rows"] == 1


# ============================================================
# parse_csv_full
# ============================================================

class TestParseCsvFull:
    def _make_csv(self, text, encoding="utf-8"):
        return text.encode(encoding)

    def _two_col_mapping(self):
        return {
            "date_col": 0,
            "desc_col": 1,
            "deposit_col": 2,
            "withdrawal_col": 3,
        }

    def _withdrawal_only_mapping(self):
        return {
            "date_col": 0,
            "desc_col": 1,
            "withdrawal_col": 2,
        }

    def test_two_column_deposit_withdrawal(self):
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,給料,300000,\n"
            "2026/01/05,食費,,5000\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 2
        assert rows[0]["date"] == date(2026, 1, 1)
        assert rows[0]["description"] == "給料"
        assert rows[0]["deposit"] == 300000
        assert rows[0]["withdrawal"] == 0
        assert rows[1]["deposit"] == 0
        assert rows[1]["withdrawal"] == 5000

    def test_skips_blank_rows(self):
        """全セル空白の行は読み飛ばす (途中の空行・末尾改行の重複)。"""
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,給料,300000,\n"
            ",,,\n"  # 全カラム空 → スキップ
            "2026/01/05,食費,,5000\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 2
        assert rows[0]["description"] == "給料"
        assert rows[1]["description"] == "食費"

    def test_negative_withdrawal_becomes_deposit(self):
        """出金列のマイナス値（キャッシュバック等）は入金に反転"""
        raw = self._make_csv(
            "日付,摘要,出金\n"
            "2026/01/01,カード利用,5000\n"
            "2026/01/05,キャッシュバック,-500\n"
        )
        rows = parse_csv_full(raw, self._withdrawal_only_mapping(), "%Y/%m/%d")
        assert len(rows) == 2
        assert rows[0]["withdrawal"] == 5000
        assert rows[0]["deposit"] == 0
        assert rows[1]["withdrawal"] == 0
        assert rows[1]["deposit"] == 500

    def test_negative_deposit_becomes_withdrawal(self):
        """入金列のマイナス値は出金に反転"""
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,振込,-1000,\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert rows[0]["deposit"] == 0
        assert rows[0]["withdrawal"] == 1000

    def test_row_num_is_1_indexed_with_header(self):
        raw = self._make_csv("日付,摘要,入金,出金\n2026/01/01,テスト,100,\n")
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert rows[0]["row_num"] == 2  # header=1, data starts at 2

    def test_raw_row_preserved(self):
        raw = self._make_csv("日付,摘要,入金,出金\n2026/01/01,テスト,100,\n")
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert rows[0]["raw_row"] == ["2026/01/01", "テスト", "100", ""]

    def test_blank_rows_skipped(self):
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,A,100,\n"
            "  ,  ,  ,  \n"
            "2026/01/02,B,,200\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 2

    def test_row_with_no_date_no_amount_skipped(self):
        """日付なし・金額0の行はスキップされる"""
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "invalid,メモのみ,,\n"
            "2026/01/01,有効,100,\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["description"] == "有効"

    def test_row_with_no_date_but_has_amount(self):
        """日付が無効でも金額があれば含まれる"""
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "invalid,メモ,1000,\n"
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["date"] is None
        assert rows[0]["deposit"] == 1000

    def test_header_only_returns_empty(self):
        raw = self._make_csv("日付,摘要,金額\n")
        rows = parse_csv_full(raw, self._withdrawal_only_mapping(), "%Y/%m/%d")
        assert rows == []

    def test_single_row_returns_empty(self):
        """ヘッダーのみ（データ行なし）"""
        raw = self._make_csv("日付,摘要,金額")
        rows = parse_csv_full(raw, self._withdrawal_only_mapping(), "%Y/%m/%d")
        assert rows == []

    def test_column_index_out_of_range(self):
        """列インデックスが範囲外の場合は空文字扱い"""
        mapping = {
            "date_col": 0,
            "desc_col": 10,  # 存在しない列
            "deposit_col": 1,
            "withdrawal_col": None,
        }
        raw = self._make_csv("日付,金額\n2026/01/01,500\n")
        rows = parse_csv_full(raw, mapping, "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["description"] == ""
        assert rows[0]["deposit"] == 500

    def test_comma_in_amount(self):
        """金額にカンマが含まれる場合"""
        raw = self._make_csv(
            '日付,摘要,金額\n'
            '2026/01/01,テスト,"1,234,567"\n'
        )
        rows = parse_csv_full(raw, self._withdrawal_only_mapping(), "%Y/%m/%d")
        assert rows[0]["withdrawal"] == 1234567

    def test_cp932_csv(self):
        raw = self._make_csv(
            "日付,摘要,入金,出金\n"
            "2026/01/01,コンビニ,,500\n",
            encoding="cp932",
        )
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 1
        assert rows[0]["description"] == "コンビニ"

    def test_yen_sign_in_amount(self):
        raw = self._make_csv(
            "日付,摘要,金額\n"
            "2026/01/01,テスト,¥1000\n"
        )
        rows = parse_csv_full(raw, self._withdrawal_only_mapping(), "%Y/%m/%d")
        assert rows[0]["withdrawal"] == 1000

    def test_multiple_rows_sequential_row_nums(self):
        lines = ["日付,摘要,入金,出金"] + [
            f"2026/01/{i:02d},item{i},{i*100}," for i in range(1, 6)
        ]
        raw = self._make_csv("\n".join(lines))
        rows = parse_csv_full(raw, self._two_col_mapping(), "%Y/%m/%d")
        assert len(rows) == 5
        assert [r["row_num"] for r in rows] == [2, 3, 4, 5, 6]


# ============================================================
# save_column_profile / load_column_profile
# ============================================================

class TestColumnProfileCrud:
    def test_save_and_load(self, db, user, accounts):
        mapping = {"date_col": 0, "desc_col": 1, "deposit_col": 2,
                   "withdrawal_col": 3}
        save_column_profile(user.id, "1020", mapping, "%Y/%m/%d")
        loaded = load_column_profile(user.id, "1020")
        assert loaded is not None
        assert loaded["date_col"] == 0
        assert loaded["desc_col"] == 1
        assert loaded["deposit_col"] == 2
        assert loaded["withdrawal_col"] == 3
        assert loaded["date_format"] == "%Y/%m/%d"

    def test_update_existing_profile(self, db, user, accounts):
        mapping1 = {"date_col": 0, "desc_col": 1, "deposit_col": 2,
                    "withdrawal_col": 3}
        save_column_profile(user.id, "1020", mapping1, "%Y/%m/%d")

        mapping2 = {"date_col": 1, "desc_col": 2,
                    "deposit_col": None, "withdrawal_col": 3}
        save_column_profile(user.id, "1020", mapping2, "%Y-%m-%d")

        loaded = load_column_profile(user.id, "1020")
        assert loaded["date_col"] == 1
        assert loaded["desc_col"] == 2
        assert loaded["deposit_col"] is None
        assert loaded["withdrawal_col"] == 3
        assert loaded["date_format"] == "%Y-%m-%d"

    def test_load_nonexistent_returns_none(self, db, user, accounts):
        assert load_column_profile(user.id, "9999") is None

    def test_different_accounts_independent(self, db, user, accounts):
        m1 = {"date_col": 0, "desc_col": 1, "deposit_col": 2,
              "withdrawal_col": 3}
        m2 = {"date_col": 1, "desc_col": 0,
              "deposit_col": None, "withdrawal_col": 2}
        save_column_profile(user.id, "1020", m1, "%Y/%m/%d")
        save_column_profile(user.id, "2010", m2, "%Y-%m-%d")

        l1 = load_column_profile(user.id, "1020")
        l2 = load_column_profile(user.id, "2010")
        assert l1["date_col"] == 0
        assert l2["date_col"] == 1
        assert l1["deposit_col"] == 2
        assert l2["deposit_col"] is None


# ============================================================
# CsvColumnProfile model
# ============================================================

class TestCsvColumnProfileModel:
    def test_to_mapping_dict(self, db, user, accounts):
        from app.models.csv_column_profile import CsvColumnProfile
        profile = CsvColumnProfile(
            user_id=user.id, account_code="1020",
            date_col=0, desc_col=1, deposit_col=2, withdrawal_col=3,
            date_format="%Y-%m-%d",
        )
        db.session.add(profile)
        db.session.commit()
        d = profile.to_mapping_dict()
        assert d["date_col"] == 0
        assert d["desc_col"] == 1
        assert d["deposit_col"] == 2
        assert d["withdrawal_col"] == 3
        assert d["date_format"] == "%Y-%m-%d"

    def test_unique_constraint(self, db, user, accounts):
        from app.models.csv_column_profile import CsvColumnProfile
        p1 = CsvColumnProfile(
            user_id=user.id, account_code="1020",
            date_col=0, desc_col=1, date_format="%Y/%m/%d",
        )
        db.session.add(p1)
        db.session.commit()

        p2 = CsvColumnProfile(
            user_id=user.id, account_code="1020",
            date_col=2, desc_col=3, date_format="%Y-%m-%d",
        )
        db.session.add(p2)
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


# ============================================================
# validate_ai_column_mapping
# ============================================================


class TestValidateAiColumnMapping:
    """LLM 出力の検証ロジック。LLM 呼出自体はクライアント側
    csv_columns_detect_orchestrator.js が行う (E2EE)。"""

    def test_normal(self):
        m = validate_ai_column_mapping({
            "date_col": 0, "desc_col": 1,
            "deposit_col": 2, "withdrawal_col": 3,
            "date_format": "%Y/%m/%d",
        }, num_cols=4)
        assert m == {
            "date_col": 0, "desc_col": 1,
            "deposit_col": 2, "withdrawal_col": 3,
            "date_format": "%Y/%m/%d",
        }

    def test_withdrawal_only(self):
        m = validate_ai_column_mapping({
            "date_col": 0, "desc_col": 1,
            "deposit_col": None, "withdrawal_col": 2,
            "date_format": "%Y/%m/%d",
        }, num_cols=3)
        assert m["deposit_col"] is None
        assert m["withdrawal_col"] == 2

    def test_out_of_range_returns_none(self):
        assert validate_ai_column_mapping({
            "date_col": 99, "desc_col": 1, "date_format": "%Y/%m/%d",
        }, num_cols=4) is None

    def test_missing_required_returns_none(self):
        assert validate_ai_column_mapping({"date_col": 0}, num_cols=4) is None

    def test_non_dict_returns_none(self):
        assert validate_ai_column_mapping(None, num_cols=4) is None
        assert validate_ai_column_mapping("x", num_cols=4) is None

    def test_default_date_format(self):
        m = validate_ai_column_mapping({
            "date_col": 0, "desc_col": 1,
        }, num_cols=2)
        assert m["date_format"] == "%Y/%m/%d"


# ============================================================
# CSV取込 mapping ビューのプロファイル統合テスト
# ============================================================

class TestCsvImportMappingProfile:
    _CSV = "日付,摘要,入金,出金\n2026/01/01,テスト,1000,\n"

    def _setup_session(self, logged_in_client):
        """セッションにCSVデータを設定"""
        import base64
        from app.views.helpers import save_import_data
        key = save_import_data({
            "raw_b64": base64.b64encode(self._CSV.encode()).decode("ascii"),
        })
        with logged_in_client.session_transaction() as sess:
            sess["csv_data_key"] = key
            sess["csv_payment_account_code"] = "1020"

    def test_mapping_get_no_profile(self, db, logged_in_client, user, accounts):
        self._setup_session(logged_in_client)
        resp = logged_in_client.get("/csv-import/mapping")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "保存済みプロファイル" not in html
        assert "AI自動検出" not in html

    def test_mapping_get_with_saved_profile(self, db, logged_in_client, user,
                                            accounts):
        mapping = {"date_col": 0, "desc_col": 1, "deposit_col": 2,
                   "withdrawal_col": 3}
        save_column_profile(user.id, "1020", mapping, "%Y/%m/%d")
        self._setup_session(logged_in_client)

        resp = logged_in_client.get("/csv-import/mapping")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "保存済みプロファイル" in html

    def test_mapping_post_saves_profile(self, db, logged_in_client, user,
                                        accounts):
        self._setup_session(logged_in_client)
        resp = logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0",
            "desc_col": "1",
            "deposit_col": "2",
            "withdrawal_col": "3",
            "date_format": "%Y/%m/%d",
        }, follow_redirects=False)
        assert resp.status_code == 302

        loaded = load_column_profile(user.id, "1020")
        assert loaded is not None
        assert loaded["date_col"] == 0
        assert loaded["desc_col"] == 1
        assert loaded["deposit_col"] == 2
        assert loaded["withdrawal_col"] == 3

    def test_mapping_post_updates_profile(self, db, logged_in_client, user,
                                          accounts):
        mapping = {"date_col": 0, "desc_col": 1, "deposit_col": 2,
                   "withdrawal_col": 3}
        save_column_profile(user.id, "1020", mapping, "%Y/%m/%d")
        self._setup_session(logged_in_client)

        resp = logged_in_client.post("/csv-import/mapping", data={
            "date_col": "0",
            "desc_col": "1",
            "deposit_col": "2",
            "withdrawal_col": "3",
            "date_format": "%Y-%m-%d",
        }, follow_redirects=False)
        assert resp.status_code == 302

        loaded = load_column_profile(user.id, "1020")
        assert loaded["date_format"] == "%Y-%m-%d"

    def test_saved_profile_with_out_of_range_col_ignored(self, db,
                                                          logged_in_client,
                                                          user, accounts):
        """列数を超えるインデックスの保存済みプロファイルは無視される"""
        mapping = {"date_col": 99, "desc_col": 1, "deposit_col": 2,
                   "withdrawal_col": 3}
        save_column_profile(user.id, "1020", mapping, "%Y/%m/%d")
        self._setup_session(logged_in_client)

        resp = logged_in_client.get("/csv-import/mapping")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "保存済みプロファイル" not in html


# ============================================================
# POST /csv-import/match/snap-date
# ============================================================

class TestSnapMatchDate:
    """E3-F PR-D-2: 日付スナップは平文 date のみ更新し暗号化 blob を放置して
    いたため削除。新フローはクライアントが復号済み entry を新日付で再暗号化し
    PUT /api/v1/journals/<id> する (期間チェック / 貸借検証は PUT 側で共通)。
    旧ルートは削除済 (404)。"""

    def test_post_snap_date_returns_404(self, logged_in_client, accounts,
                                        account_types):
        resp = logged_in_client.post(
            "/csv-import/match/snap-date",
            json={"entry_id": 1, "csv_date": "2026-05-03"},
        )
        assert resp.status_code == 404


# ============================================================
# POST /csv-import/api/columns-detect-context
# ============================================================


class TestColumnsDetectContext:
    """クライアント側 LLM 呼出のためのプロンプト材料配信エンドポイント。"""

    def test_unauthenticated(self, client):
        resp = client.post("/csv-import/api/columns-detect-context",
                           json={"headers": ["a"], "sample_rows": []})
        assert resp.status_code in (302, 401)

    def test_returns_template_and_metadata(
        self, logged_in_client, accounts,
    ):
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={
                "headers": ["日付", "摘要", "入金", "出金"],
                "sample_rows": [["2026/01/01", "テスト", "1000", ""]],
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # placeholder 3 種
        assert "__HEADERS_TEXT__" in body["prompt_template"]
        assert "__SAMPLE_TEXT__" in body["prompt_template"]
        assert "__SAMPLE_COUNT__" in body["prompt_template"]
        # サーバ側で構築済の headers_text / sample_text
        assert "[0] 日付" in body["headers_text"]
        assert "[1] 摘要" in body["headers_text"]
        assert "テスト" in body["sample_text"]
        assert body["sample_count"] == 1
        assert body["num_cols"] == 4
        # default_model_by_provider
        from app.services.ai_receipt import PROVIDER_DEFAULTS
        for k in ("openai", "anthropic", "google"):
            assert body["default_model_by_provider"][k] == PROVIDER_DEFAULTS[k]
        assert "llama_cpp" not in body["default_model_by_provider"]
        # custom_prompt (UserAIConfig 未設定なら空文字)
        assert body["custom_prompt"] == ""
        # api_key 関連は一切返却しない
        assert "api_key_blob" not in body

    def test_invalid_headers_returns_400(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={"headers": [], "sample_rows": []},
        )
        assert resp.status_code == 400

    def test_non_list_sample_rows_returns_400(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={"headers": ["a"], "sample_rows": "not-list"},
        )
        assert resp.status_code == 400

    def test_too_many_headers_returns_400(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={"headers": [f"col{i}" for i in range(51)], "sample_rows": []},
        )
        assert resp.status_code == 400
        assert "exceeds maximum" in resp.get_json()["error"]

    def test_header_too_long_returns_400(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={"headers": ["x" * 201], "sample_rows": []},
        )
        assert resp.status_code == 400

    def test_non_string_header_returns_400(self, logged_in_client, accounts):
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={"headers": [123, "ok"], "sample_rows": []},
        )
        assert resp.status_code == 400

    def test_long_cell_truncated_to_max_cell_len(
        self, logged_in_client, accounts,
    ):
        long_cell = "x" * 5000
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={
                "headers": ["a", "b"],
                "sample_rows": [[long_cell, "ok"]],
            },
        )
        assert resp.status_code == 200
        # MAX_CELL_LEN=1000 で切り詰められ、5000 x が残らない
        assert "x" * 2000 not in resp.get_json()["sample_text"]
        assert "x" * 1000 in resp.get_json()["sample_text"]

    def test_wide_row_capped_to_max_headers(
        self, logged_in_client, accounts,
    ):
        # sample_rows の 1 行に MAX_HEADERS (=50) を超える列を送っても
        # sample_text 側で MAX_HEADERS 個までに切り詰められること
        # (10MB 級の sample_text 肥大化を防ぐ。headers と対応する列だけ
        #  あれば LLM 推定には十分。)
        resp = logged_in_client.post(
            "/csv-import/api/columns-detect-context",
            json={
                "headers": ["a", "b"],
                "sample_rows": [[f"v{i}" for i in range(200)]],
            },
        )
        assert resp.status_code == 200
        sample_text = resp.get_json()["sample_text"]
        # v0..v49 (MAX_HEADERS=50) は含むが v50 以降は含まない
        assert "v49" in sample_text
        assert "v50" not in sample_text

