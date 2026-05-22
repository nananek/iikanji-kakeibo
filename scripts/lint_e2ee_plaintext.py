#!/usr/bin/env python3
"""E2EE 平文鍵漏洩 lint (AST ベース)。

検出対象:
- logger.* / print / current_app.logger.* に raw key 変数が渡される
- jsonify / make_response の kwarg 名や dict キーに raw key 名がある
- f-string で raw key 変数を展開

検出しない (誤検知削減):
- 文字列リテラル中に keyword が現れるだけ (例: error="IV length is bad")
- `# noqa: e2ee-lint` コメント付き行
- DB row 経由の attribute 読み出し (例: row.wrapped_master_key)
  ※ 暗号文を扱う API では intended なので OK

命名規約:
raw key を扱う変数名は RAW_KEY_NAMES に列挙された名前を使う。それ以外の
変数名 (mk, key, ...) は本 lint では検出されないので、開発者は raw key
を扱うときは raw_<thing> / plaintext_<thing> / derived_<thing> 等の明確な
命名を使うこと。

使い方:
  python scripts/lint_e2ee_plaintext.py app/

戻り値: 違反検出時 1、なし時 0。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


# 「これらの変数名が logger/print/jsonify/make_response に渡されたら違反」
# というブラックリスト。raw key 系は接頭辞で識別する命名規約を採用。
# - master_key (raw MK)、migration_temp_mk (一時 MK)
# - derived_key_* (KDF 派生鍵): passkey / pw / recovery
# - raw_seed / plaintext_mnemonic (BIP-39 24 単語の平文)
# - mnemonic_plain (同上)
RAW_KEY_NAMES = frozenset({
    "master_key",
    "mk_plain",
    "plaintext_mk",
    "raw_mk",
    "raw_master_key",
    "migration_temp_mk",
    "derived_key",
    "derived_key_passkey",
    "derived_key_pw",
    "derived_key_recovery",
    "raw_seed",
    "plain_seed",
    "plaintext_mnemonic",
    "mnemonic_plain",
})

# logger メソッド名
LOGGER_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "critical", "exception",
})

# レスポンス系関数 (kwarg 名 / dict キーをチェック)
RESPONSE_FN_NAMES = frozenset({"jsonify", "make_response"})

# noqa マーカー
NOQA_MARKER = "noqa: e2ee-lint"


class E2EELinter(ast.NodeVisitor):
    def __init__(self, source_lines: list[str], filename: Path):
        self.violations: list[tuple[int, str]] = []
        self.source_lines = source_lines
        self.filename = filename

    def visit_Call(self, node: ast.Call) -> None:
        fn_name = _call_func_name(node)
        is_logger = self._is_logger_call(node) or fn_name == "print"
        is_response = fn_name in RESPONSE_FN_NAMES

        if is_logger or is_response:
            # 位置引数の検査
            for arg in node.args:
                self._check_value_node(arg, node.lineno, fn_name)
            # キーワード引数の検査
            for kw in node.keywords:
                # kwarg 名自体が raw key 名か (例: jsonify(master_key=...))
                if kw.arg and kw.arg in RAW_KEY_NAMES:
                    self._record(
                        node.lineno,
                        f"kwarg name {kw.arg!r} is a raw key in {fn_name}()",
                    )
                # kwarg 値も検査
                self._check_value_node(kw.value, node.lineno, fn_name)

        self.generic_visit(node)

    def _is_logger_call(self, node: ast.Call) -> bool:
        """`xxx.info(...)` 等の logger 呼び出しを判定。"""
        if not isinstance(node.func, ast.Attribute):
            return False
        return node.func.attr in LOGGER_METHODS

    def _check_value_node(
        self, node: ast.expr, lineno: int, fn_name: str
    ) -> None:
        """位置引数 / kwarg 値の中に raw key 名の参照があるかを検査。"""
        if self._noqa_on_line(lineno):
            return

        # 単純な Name 参照: logger.info(master_key) → 違反
        if isinstance(node, ast.Name) and node.id in RAW_KEY_NAMES:
            self._record(
                lineno,
                f"raw key {node.id!r} passed to {fn_name}()",
            )
            return

        # dict literal: jsonify({"master_key": ...}) → 違反
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in RAW_KEY_NAMES
                ):
                    self._record(
                        lineno,
                        f"dict key {key.value!r} is a raw key in {fn_name}()",
                    )

        # f-string: logger.info(f"mk={master_key}") → 違反
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue) and isinstance(
                    value.value, ast.Name
                ):
                    if value.value.id in RAW_KEY_NAMES:
                        self._record(
                            lineno,
                            f"raw key {value.value.id!r} in f-string of {fn_name}()",
                        )

    def _noqa_on_line(self, lineno: int) -> bool:
        if 0 < lineno <= len(self.source_lines):
            return NOQA_MARKER in self.source_lines[lineno - 1]
        return False

    def _record(self, lineno: int, msg: str) -> None:
        self.violations.append((lineno, msg))


def _call_func_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def lint_file(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [(exc.lineno or 0, f"syntax error: {exc.msg}")]
    linter = E2EELinter(src.splitlines(), path)
    linter.visit(tree)
    return linter.violations


def _collect_paths(args: list[str]) -> list[Path]:
    paths: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_file() and p.suffix == ".py":
            paths.append(p)
        elif p.is_dir():
            paths.extend(p.rglob("*.py"))
    return paths


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: lint_e2ee_plaintext.py <dir or file> [...]",
            file=sys.stderr,
        )
        return 2

    paths = _collect_paths(argv[1:])
    if not paths:
        print("no Python files to lint", file=sys.stderr)
        return 0

    exit_code = 0
    for path in paths:
        for lineno, msg in lint_file(path):
            print(f"{path}:{lineno}: {msg}")
            exit_code = 1

    if exit_code == 0:
        print("✓ no E2EE raw key leaks detected")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
