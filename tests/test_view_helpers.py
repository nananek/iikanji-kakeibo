"""views/helpers.py のテスト"""

from app.views.helpers import safe_user_error


class TestSafeUserError:
    def test_passes_short_message(self):
        e = ValueError("勘定科目が見つかりません")
        assert safe_user_error(e) == "勘定科目が見つかりません"

    def test_blocks_traceback_token(self):
        e = RuntimeError('Traceback (most recent call last):\n  File "..."')
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_file_path_token(self):
        e = ValueError("error at /app/views/journal.py")
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_sqlalchemy(self):
        e = RuntimeError("sqlalchemy.exc.OperationalError: ...")
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_psycopg(self):
        e = RuntimeError("psycopg2.OperationalError: timeout")
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_overlong(self):
        e = ValueError("x" * 300)
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_newline(self):
        e = ValueError("line1\nline2")
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_carriage_return(self):
        e = ValueError("line1\rline2")
        assert safe_user_error(e) == "処理に失敗しました"

    def test_blocks_empty(self):
        e = ValueError()
        assert safe_user_error(e) == "処理に失敗しました"

    def test_custom_fallback(self):
        e = ValueError()
        assert safe_user_error(e, fallback="代替") == "代替"

    def test_non_string_first_arg(self):
        e = ValueError(123)
        assert safe_user_error(e) == "処理に失敗しました"
