"""ストレージ整合性監査 (Phase 5 #70)。

`file_size` NULL のレガシー Voucher / AIDraft をストレージから実測して
埋め、`StorageUsage` の集計値と実測合計の drift を検出する。`fix` モード
で StorageUsage を実測値に同期する。

呼び出し: `flask storage-audit` / `flask storage-audit --fix`
"""

from sqlalchemy import func as sa_func

from app.extensions import db
from app.models.ai_draft import AIDraft
from app.models.storage import StorageUsage
from app.models.user import User
from app.models.voucher import Voucher
from app.services.storage import get_storage_backend


def backfill_file_sizes() -> dict:
    """`file_size IS NULL` な Voucher / AIDraft をストレージから実測して埋める。

    Returns:
        ``{"voucher_backfilled": int, "draft_backfilled": int, "errors": list[str]}``
    """
    backend = get_storage_backend()
    stats: dict = {
        "voucher_backfilled": 0,
        "draft_backfilled": 0,
        "errors": [],
    }

    vouchers = Voucher.query.filter(Voucher.file_size.is_(None)).all()
    for v in vouchers:
        try:
            v.file_size = len(backend.get(v.image_key))
            stats["voucher_backfilled"] += 1
        except Exception as e:  # noqa: BLE001 — best-effort scan
            stats["errors"].append(f"voucher {v.id}: {e}")

    drafts = AIDraft.query.filter(AIDraft.file_size.is_(None)).all()
    for d in drafts:
        try:
            d.file_size = len(backend.get(d.image_key))
            stats["draft_backfilled"] += 1
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"draft {d.id}: {e}")

    db.session.commit()
    return stats


def measure_user_usage(user_id: int) -> int:
    """ユーザーの実測使用量 = Voucher.file_size 合計 + AIDraft.file_size 合計."""
    v_sum = db.session.query(
        sa_func.coalesce(sa_func.sum(Voucher.file_size), 0)
    ).filter(Voucher.user_id == user_id).scalar() or 0
    d_sum = db.session.query(
        sa_func.coalesce(sa_func.sum(AIDraft.file_size), 0)
    ).filter(AIDraft.user_id == user_id).scalar() or 0
    return int(v_sum) + int(d_sum)


def audit_storage_usage(fix: bool = False) -> dict:
    """各ユーザーの `StorageUsage.used_bytes` を実測合計と比較する。

    Args:
        fix: True で drift を解消するよう `StorageUsage` を実測値で上書き。

    Returns:
        ``{"users_checked": int, "drift_detected": int, "drift_fixed": int,
          "drifts": [{"user_id", "measured", "recorded", "delta"}, ...]}``
    """
    stats: dict = {
        "users_checked": 0,
        "drift_detected": 0,
        "drift_fixed": 0,
        "drifts": [],
    }
    users = User.query.all()
    for user in users:
        stats["users_checked"] += 1
        measured = measure_user_usage(user.id)
        usage = db.session.get(StorageUsage, user.id)
        recorded = usage.used_bytes if usage else 0
        if measured == recorded:
            continue
        stats["drift_detected"] += 1
        stats["drifts"].append({
            "user_id": user.id,
            "measured": measured,
            "recorded": recorded,
            "delta": measured - recorded,
        })
        if fix:
            if usage is None:
                db.session.add(
                    StorageUsage(user_id=user.id, used_bytes=measured)
                )
            else:
                usage.used_bytes = measured
            stats["drift_fixed"] += 1
    if fix:
        db.session.commit()
    return stats
