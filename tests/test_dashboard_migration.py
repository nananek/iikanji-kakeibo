"""E7 (#114): ダッシュボードの temp-MK 再ラップ移行バナー表示テスト。"""

from app.models.balance_cache import BalanceCacheBlob
from tests.conftest import make_journal


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
    return client


def test_banner_shown_when_temp_mk_active(db, client, user):
    user.migration_temp_mk = bytes(range(32))
    # je と bcb で年度を作る (migration_years の和集合)
    make_journal(db, user.id, "1010", "5010", 1000,
                 entry_date=__import__("datetime").date(2025, 5, 1))
    db.session.add(BalanceCacheBlob(
        user_id=user.id, year=2024, period=3,
        encrypted_blob=b"x", blob_iv=bytes(12),
    ))
    db.session.commit()

    _login(client, user)
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "migration-rewrap-banner" in html
    assert "migration-rewrap-params" in html
    # je(2025) と bcb(2024) の和集合が years に入る
    assert "2024" in html
    assert "2025" in html


def test_banner_absent_when_no_temp_mk(db, client, user):
    user.migration_temp_mk = None
    db.session.commit()
    _login(client, user)
    r = client.get("/")
    assert r.status_code == 200
    assert "migration-rewrap-banner" not in r.get_data(as_text=True)
