"""OAuth / MCP で共通に使うスコープ定義。

REST API の API キースコープ (app/models/api_key.py) に加え、MCP
サーバー専用の ``accounts:read`` を扱う。認可画面ではここに定義した
スコープをチェックボックスで提示し、ユーザーが承認時に絞り込める。
"""

# scope 名 -> 日本語の説明 (認可画面の表示用)
SCOPES: dict[str, str] = {
    "journals:read": "仕訳・証憑の閲覧",
    "journals:create": "仕訳の起票",
    "journals:delete": "仕訳の削除",
    "ai:analyze": "AI証憑仕訳（解析・下書き操作）",
    "reports:read": "レポート（試算表・損益・確定申告）の閲覧",
    "accounts:read": "勘定科目の閲覧",
}

# 書き込みを伴うスコープ。1 つも選ばれなければ実質読み取り専用になる。
WRITE_SCOPES = frozenset({"journals:create", "journals:delete", "ai:analyze"})

# 読み取り専用スコープ。クライアントがスコープを要求しなかったときの
# 既定値にする (最小権限)。書き込みは明示的に選ばせて付与する。
READ_SCOPES = tuple(s for s in SCOPES if s not in WRITE_SCOPES)
DEFAULT_SCOPES = READ_SCOPES

# 既定で全スコープを表す並び (表示順)
ALL_SCOPES = tuple(SCOPES.keys())


def is_write_scope(scope: str) -> bool:
    return scope in WRITE_SCOPES


def read_only_for(scopes) -> bool:
    """選択スコープに書き込み権限が含まれなければ True。"""
    if scopes is None:
        return False
    return not any(is_write_scope(s) for s in scopes)


def normalize(scope_str) -> list[str]:
    """スペース区切りの scope 文字列を、既知スコープのリストに正規化する。"""
    if not scope_str:
        return []
    seen = []
    for token in str(scope_str).replace(",", " ").split():
        if token in SCOPES and token not in seen:
            seen.append(token)
    return seen


def describe(scopes) -> list[dict]:
    """scope リストを {name, description} のリストに変換する。"""
    return [{"name": s, "description": SCOPES.get(s, "")} for s in scopes]
