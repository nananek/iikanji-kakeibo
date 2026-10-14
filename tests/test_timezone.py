"""アプリ基準タイムゾーン (app/services/clock.py) の回帰テスト。

`date.today()` はプロセスのタイムゾーンに依存するため、UTC 運用の
コンテナでは「0時を過ぎたのに前日の日付が初期値になる」事故が起きる。
アプリは APP_TIMEZONE (既定 Asia/Tokyo) を明示して日付を決める。
"""

from datetime import date, datetime, timezone

from app.services import clock


def test_today_follows_configured_timezone(app, monkeypatch):
    """UTC 15:30 は JST では翌日 0:30 — 既定 Asia/Tokyo なら翌日になる。"""
    monkeypatch.setitem(app.config, "APP_TIMEZONE", "Asia/Tokyo")
    frozen = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(clock, "datetime", _FrozenDateTime)

    with app.app_context():
        # UTC ではまだ 1/1 だが、JST では 1/2
        assert clock.today() == date(2026, 1, 2)


def test_today_utc_timezone(app, monkeypatch):
    monkeypatch.setitem(app.config, "APP_TIMEZONE", "UTC")
    frozen = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc)

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    monkeypatch.setattr(clock, "datetime", _FrozenDateTime)

    with app.app_context():
        assert clock.today() == date(2026, 1, 1)


def test_invalid_timezone_falls_back_to_utc(app, monkeypatch):
    monkeypatch.setitem(app.config, "APP_TIMEZONE", "Not/AZone")
    with app.app_context():
        assert clock.get_zone() == timezone.utc


def test_to_app_tz_normalizes_naive_as_utc(app, monkeypatch):
    monkeypatch.setitem(app.config, "APP_TIMEZONE", "Asia/Tokyo")
    with app.app_context():
        # naive は UTC とみなして JST に変換 (9 時間進む)
        result = clock.to_app_tz(datetime(2026, 1, 1, 0, 0))
        assert result.utcoffset().total_seconds() == 9 * 3600
        assert result.date() == date(2026, 1, 1)
        assert clock.to_app_tz(None) is None


def test_cashbook_new_defaults_to_app_today(
    logged_in_client, app, accounts, monkeypatch
):
    from app.views import cashbook as cashbook_view

    monkeypatch.setitem(app.config, "APP_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setattr(cashbook_view, "app_today", lambda: date(2030, 5, 6))
    resp = logged_in_client.get("/cashbook/new")
    assert resp.status_code == 200
    assert 'value="2030-05-06"' in resp.get_data(as_text=True)


def test_journal_new_defaults_to_app_today(
    logged_in_client, app, accounts, monkeypatch
):
    from app.views import journal as journal_view

    monkeypatch.setitem(app.config, "APP_TIMEZONE", "Asia/Tokyo")
    monkeypatch.setattr(journal_view, "app_today", lambda: date(2030, 5, 6))
    resp = logged_in_client.get("/journal/new")
    assert resp.status_code == 200
    assert 'value="2030-05-06"' in resp.get_data(as_text=True)
