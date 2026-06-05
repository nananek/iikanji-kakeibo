"""E7 (#114) §16.6: E2EE 一斉移行の進捗集計。

`flask migration-status` CLI (§16.6) と `/admin/migration-progress` ダッシュボード
(§16.6) が同じ集計を使えるよう、生のカウントを 1 箇所で算出する。鍵設定済み
判定は `public_key IS NOT NULL` (= 鍵ペア生成済み)、ロック判定は
`is_active=False` (鍵未設定ロック §16.5)、移行待ち判定は `migration_temp_mk
IS NOT NULL` (= まだサーバ temp-MK を保持＝再ラップ未完) で揃える。
"""

from app.models.user import User


def compute_migration_counts() -> dict:
    """personal ユーザーについて移行関連の生カウントを返す。

    返すキー (全て int):
      - total           : personal ユーザー総数
      - with_keys       : public_key 設定済み (鍵設定完了)
      - without_keys    : 鍵未設定 (= total - with_keys)
      - locked          : is_active=False (鍵未設定ロック中)
      - temp_mk_holders : migration_temp_mk 保持中 (再ラップ未完=移行待ち)
    """
    base = User.query.filter_by(user_type="personal")
    total = base.count()
    with_keys = base.filter(User.public_key.isnot(None)).count()
    locked = base.filter(User.is_active.is_(False)).count()
    temp_mk_holders = base.filter(User.migration_temp_mk.isnot(None)).count()
    return {
        "total": int(total),
        "with_keys": int(with_keys),
        "without_keys": int(total - with_keys),
        "locked": int(locked),
        "temp_mk_holders": int(temp_mk_holders),
    }


def migration_progress_report() -> dict:
    """§16.6 ダッシュボード/API 用に整形した進捗レポートを返す。

    `data_re_encrypted_pct` は per-data の進捗列を持たない設計のため、鍵設定
    済み率 (鍵設定は再暗号化の前提) を proxy として用いる。
    """
    c = compute_migration_counts()
    total = c["total"]
    pct = round(c["with_keys"] / total * 100, 1) if total else 0.0
    return {
        "total_users": c["total"],
        "users_with_keys": c["with_keys"],
        "users_without_keys": c["without_keys"],
        "users_locked": c["locked"],
        "users_pending": c["temp_mk_holders"],
        "data_re_encrypted_pct": pct,
        "temp_mk_active": c["temp_mk_holders"] > 0,
        "temp_mk_finalize_eligible": c["temp_mk_holders"] == 0,
    }
