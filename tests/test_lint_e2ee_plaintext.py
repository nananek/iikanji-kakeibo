"""scripts/lint_e2ee_plaintext.py の単体テスト。

AST ベース lint が正しく:
- raw key 変数の logger/jsonify への伝搬を検出する
- 文字列リテラル中の keyword は誤検知しない
- # noqa: e2ee-lint で抑制できる
ことを検証する。
"""

import importlib.util
import sys
from pathlib import Path
from textwrap import dedent

import pytest


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "lint_e2ee_plaintext.py"


def _load_lint_module():
    spec = importlib.util.spec_from_file_location("lint_e2ee_plaintext", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lint = _load_lint_module()


def _lint_source(src: str, tmp_path: Path) -> list[tuple[int, str]]:
    p = tmp_path / "sample.py"
    p.write_text(dedent(src))
    return lint.lint_file(p)


# --- 違反として検出すべきケース ---


def test_logger_with_raw_master_key_positional(tmp_path):
    src = """
        import logging
        logger = logging.getLogger()
        master_key = b"..."
        logger.info("MK is %s", master_key)
    """
    v = _lint_source(src, tmp_path)
    assert len(v) == 1
    assert "master_key" in v[0][1]


def test_logger_with_raw_key_in_fstring(tmp_path):
    src = """
        import logging
        logger = logging.getLogger()
        derived_key = b"..."
        logger.debug(f"derived={derived_key}")
    """
    v = _lint_source(src, tmp_path)
    assert len(v) == 1
    assert "derived_key" in v[0][1]


def test_print_with_raw_key(tmp_path):
    src = """
        raw_seed = "abandon abandon ..."
        print(raw_seed)
    """
    v = _lint_source(src, tmp_path)
    assert len(v) == 1
    assert "raw_seed" in v[0][1]


def test_jsonify_kwarg_name_is_raw_key(tmp_path):
    src = """
        from flask import jsonify
        mk_value = b"..."
        return jsonify(master_key=mk_value)
    """
    v = _lint_source(src, tmp_path)
    assert any("master_key" in msg for _, msg in v)


def test_jsonify_dict_key_is_raw_key(tmp_path):
    src = """
        from flask import jsonify
        mk_value = b"..."
        return jsonify({"master_key": mk_value})
    """
    v = _lint_source(src, tmp_path)
    assert any("master_key" in msg for _, msg in v)


def test_make_response_with_raw_key(tmp_path):
    src = """
        from flask import make_response
        derived_key = b"..."
        return make_response({"derived_key": derived_key})
    """
    v = _lint_source(src, tmp_path)
    assert len(v) >= 1


def test_migration_temp_mk_in_logger(tmp_path):
    src = """
        import logging
        logger = logging.getLogger()
        migration_temp_mk = b"..."
        logger.error("migration broke: %s", migration_temp_mk)
    """
    v = _lint_source(src, tmp_path)
    assert any("migration_temp_mk" in msg for _, msg in v)


# --- 誤検知しないべきケース ---


def test_string_literal_with_keyword_text(tmp_path):
    """文字列リテラル内に keyword が現れるだけなら誤検知しない (#134 のケース)。"""
    src = """
        from flask import jsonify
        return jsonify(error="wrap_iv must be 12 bytes")
    """
    v = _lint_source(src, tmp_path)
    assert v == []


def test_safe_attribute_access(tmp_path):
    """row.wrapped_master_key 等の attribute アクセスは検出しない (暗号文を扱う API は intended)。"""
    src = """
        from flask import jsonify
        return jsonify(
            wrapped_master_key=row.wrapped_master_key,
            wrap_iv=row.wrap_iv,
        )
    """
    v = _lint_source(src, tmp_path)
    assert v == []  # row.* の属性アクセスは Name ノードでないので検出されない


def test_method_string_literal(tmp_path):
    """method="recovery_seed" のような API contract 文字列は OK。"""
    src = """
        from flask import jsonify
        if method == "recovery_seed":
            return jsonify(method="recovery_seed")
    """
    v = _lint_source(src, tmp_path)
    assert v == []


def test_noqa_suppression(tmp_path):
    """# noqa: e2ee-lint で抑制できる。"""
    src = """
        import logging
        logger = logging.getLogger()
        master_key = b"..."
        logger.info("debug only %s", master_key)  # noqa: e2ee-lint
    """
    v = _lint_source(src, tmp_path)
    assert v == []


def test_unrelated_variable_name(tmp_path):
    """RAW_KEY_NAMES にない変数名は検出しない (例: my_key)。"""
    src = """
        import logging
        logger = logging.getLogger()
        my_key = b"..."
        logger.info("foo: %s", my_key)
    """
    v = _lint_source(src, tmp_path)
    assert v == []


def test_unrelated_function(tmp_path):
    """logger/print/jsonify/make_response 以外なら検出しない。"""
    src = """
        master_key = b"..."
        some_other_function(master_key)
    """
    v = _lint_source(src, tmp_path)
    assert v == []


# --- 実プロジェクト統合 ---


def test_lint_runs_on_app_dir():
    """app/ 全体に対して lint を走らせて違反 0 を確認。"""
    app_dir = SCRIPT_PATH.parent.parent / "app"
    assert app_dir.is_dir()
    violations: list[tuple[Path, int, str]] = []
    for py in app_dir.rglob("*.py"):
        for lineno, msg in lint.lint_file(py):
            violations.append((py, lineno, msg))
    assert violations == [], (
        f"Unexpected violations in app/: {violations[:5]}"
    )
