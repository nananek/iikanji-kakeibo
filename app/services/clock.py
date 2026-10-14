"""アプリ基準の現在日時を一元管理するヘルパー。

``date.today()`` / ``datetime.now()`` はプロセスのシステムタイムゾーンに
依存する。コンテナの ``TZ`` 設定漏れや UTC 運用時に「0時を過ぎたのに前日の
日付が初期値になる」といった日付ズレ事故が起きるため、本モジュールで
アプリの基準タイムゾーン (``APP_TIMEZONE``) を明示して日付・時刻を取得する。

- 既定タイムゾーンは ``Asia/Tokyo``。
- ``APP_TIMEZONE`` に不正な値を設定した場合は UTC にフォールバックする。
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Tokyo"


def _zone_name() -> str:
    try:
        from flask import current_app, has_app_context
    except Exception:  # pragma: no cover - Flask 未初期化時のフォールバック
        return DEFAULT_TIMEZONE
    if has_app_context():
        name = current_app.config.get("APP_TIMEZONE")
        if name:
            return name
    return DEFAULT_TIMEZONE


def get_zone():
    """アプリ基準の tzinfo を返す。解決できなければ UTC。"""
    try:
        return ZoneInfo(_zone_name())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc


def now() -> datetime:
    """アプリ基準タイムゾーンの現在時刻 (tz-aware)。"""
    return datetime.now(get_zone())


def today():
    """アプリ基準タイムゾーンの今日の日付 (date)。"""
    return now().date()


def to_app_tz(dt: datetime | None) -> datetime | None:
    """naive な datetime は UTC とみなし、アプリ基準タイムゾーンへ変換する。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_zone())
