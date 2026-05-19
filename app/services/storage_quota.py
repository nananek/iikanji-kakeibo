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
from sqlalchemy import case, func, update

from app.extensions import db
from app.models.storage import StorageUsage
from app.services.entitlement import has_entitlement


class QuotaExceededError(Exception):
    """容量上限を超える / 未契約ユーザーがアップロードを試みた."""


def get_quota_bytes(user) -> int:
    """ユーザーの容量上限 (bytes)。

    現状は全ユーザー共通の `STORAGE_QUOTA_BYTES_DEFAULT` 値を返す。
    将来 `storage_extra` 等の追加プランで per-user 上限を実装する場合の
    拡張点として `user` 引数を保持している。
    """
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
    `QuotaExceededError` を送出する。

    本関数は事前判定のみで並行リクエスト下では TOCTOU レースが残る。
    最終的な整合性は `record_upload` 側のアトミック UPDATE と
    呼出側で記録後に上限を再検証する楽観的パターンで担保すること。
    """
    if incoming_size <= 0:
        raise ValueError(
            f"incoming_size must be positive, got {incoming_size}"
        )
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
    """アップロード成功後の使用量加算。

    `UPDATE ... SET used_bytes = used_bytes + :size` のアトミック更新で、
    並行リクエスト下でも加算が消失しない。レコードが存在しない場合は
    新規 INSERT (`rowcount == 0` で判定)。

    注意: 同一ユーザーの **初回** アップロードが並行する稀ケースで
    `UPDATE → rowcount==0 → INSERT` の競合が起こり、後発リクエストが
    UNIQUE 制約違反 (IntegrityError) になる可能性がある。アップロード
    エンドポイント統合 PR (Phase 5 続編) で PostgreSQL の
    `INSERT ... ON CONFLICT (user_id) DO UPDATE` 化、または
    SAVEPOINT + retry での共通 upsert ヘルパーに置き換える予定。
    現状の基盤 PR ではエンドポイント未統合のため実害はない。
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    result = db.session.execute(
        update(StorageUsage)
        .where(StorageUsage.user_id == user.id)
        .values(
            used_bytes=StorageUsage.used_bytes + size,
            # Core UPDATE は ORM の `onupdate` フックをバイパスするため
            # ここで明示的に更新する。SQLite/PostgreSQL 共通動作。
            updated_at=func.now(),
        )
    )
    if result.rowcount == 0:
        db.session.add(StorageUsage(user_id=user.id, used_bytes=size))
    db.session.commit()


def record_delete(user, size: int) -> None:
    """削除後の使用量減算 (0 未満にはしない)。

    `CASE WHEN used_bytes >= :size THEN used_bytes - :size ELSE 0 END`
    で SQLite/PostgreSQL の両方で動くアトミック更新。レコードがない
    ケースは整合性異常の可能性があるため warning ログを残す。
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    result = db.session.execute(
        update(StorageUsage)
        .where(StorageUsage.user_id == user.id)
        .values(
            used_bytes=case(
                (StorageUsage.used_bytes >= size,
                 StorageUsage.used_bytes - size),
                else_=0,
            ),
            updated_at=func.now(),
        )
    )
    if result.rowcount == 0:
        current_app.logger.warning(
            "record_delete called for user_id=%d but StorageUsage row "
            "does not exist (size=%d)", user.id, size,
        )
    db.session.commit()
