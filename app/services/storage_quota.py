"""ストレージクオータ管理 (Phase 5 #70)。

無償ユーザーは `voucher_storage` エンタイトルメントを持たないため証憑
画像を永続保管できない (アップロード自体を拒否)。有償ユーザーは
`StorageUsage` テーブルで集計しつつ、上限を超えるアップロードを拒否
する。

アップロードエンドポイント側で `check_quota(user, size)` を事前呼び出し
し、成功時に `record_upload(user, size)` で加算。削除時には
`record_delete(user, size)` で減算する。
"""

from flask import current_app

from app.extensions import db
from app.models.storage import StorageUsage
from app.services.entitlement import has_entitlement


class QuotaExceededError(Exception):
    """容量上限を超える / 未契約ユーザーがアップロードを試みた."""


def get_quota_bytes(user) -> int:
    """ユーザーの容量上限 (bytes)."""
    return int(
        current_app.config.get(
            "STORAGE_QUOTA_BYTES_DEFAULT", 500 * 1024 * 1024
        )
    )


def get_used_bytes(user) -> int:
    """ユーザーの現在使用バイト数 (レコードがない場合は 0)."""
    row = db.session.get(StorageUsage, user.id)
    return row.used_bytes if row else 0


def check_quota(user, incoming_size: int) -> None:
    """容量チェック。`voucher_storage` 未契約か上限超過で
    `QuotaExceededError` を送出する。"""
    if not has_entitlement(user, "voucher_storage"):
        raise QuotaExceededError(
            "証憑画像の永続保管には有償プラン (voucher_storage) が必要です。"
        )
    used = get_used_bytes(user)
    quota = get_quota_bytes(user)
    if used + incoming_size > quota:
        raise QuotaExceededError(
            f"容量上限 ({quota // (1024 * 1024)} MB) を超えます。"
            f"現在 {used // (1024 * 1024)} MB / "
            f"{quota // (1024 * 1024)} MB 使用中。"
        )


def record_upload(user, size: int) -> None:
    """アップロード成功後の使用量加算 (commit する)."""
    row = db.session.get(StorageUsage, user.id)
    if row is None:
        row = StorageUsage(user_id=user.id, used_bytes=size)
        db.session.add(row)
    else:
        row.used_bytes += size
    db.session.commit()


def record_delete(user, size: int) -> None:
    """削除後の使用量減算 (0 未満にはしない)."""
    row = db.session.get(StorageUsage, user.id)
    if row is None:
        return
    row.used_bytes = max(0, row.used_bytes - size)
    db.session.commit()
