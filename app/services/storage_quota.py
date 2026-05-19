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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.extensions import db
from app.models.storage import StorageUsage
from app.services.entitlement import has_entitlement


class QuotaExceededError(Exception):
    """容量上限を超える / 未契約ユーザーがアップロードを試みた。

    `user_message` 属性に「ユーザーに見せて良い固定文言」のみを格納する。
    view からは `str(exc)` ではなく `exc.user_message` を返すこと。
    `str(exc)` 経由だと CodeQL `py/stack-trace-exposure` が誤検出する
    (本クラスは自前文言を渡しているだけで stack trace は含まれないが、
    Exception サブクラスの `__str__` は静的解析で suspicious 扱いになる)。
    """

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


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


def get_storage_summary(user):
    """設定画面・残量表示 UI 向けのサマリを返す。

    `voucher_storage` エンタイトルメントを持たないユーザーは証憑の
    永続保管自体が許可されていないため、容量メーター表示は意味を
    持たない。その場合は `None` を返し、テンプレート側で
    `{% if storage_summary %}` でセクションごと非表示にする。

    返り値 (契約者向け):
        - `used_bytes` / `quota_bytes`: 現在使用量 / 上限 (bytes)
        - `used_mb` / `quota_mb`: 人間向け表示用 (小数 1 桁)
        - `percentage`: 通常 0〜100 の数値 (小数 1 桁)。TOCTOU 等で
          `used_bytes > quota_bytes` になった場合は 100 を超え得るため、
          テンプレート側で `[..., 100] | min` でキャップして表示する。
        - `level`: `"ok"` (<80%) / `"warning"` (80–94.9%) / `"critical"` (≥95%)
    """
    if not has_entitlement(user, "voucher_storage"):
        return None
    used = get_used_bytes(user)
    quota = get_quota_bytes(user)
    pct = round((used / quota) * 100, 1) if quota > 0 else 0
    if pct >= 95:
        level = "critical"
    elif pct >= 80:
        level = "warning"
    else:
        level = "ok"
    mb = 1024 * 1024
    return {
        "used_bytes": used,
        "quota_bytes": quota,
        "used_mb": round(used / mb, 1),
        "quota_mb": round(quota / mb, 1),
        "percentage": pct,
        "level": level,
    }


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


def _upsert_storage_usage(user_id: int, delta: int) -> None:
    """`INSERT ... ON CONFLICT (user_id) DO UPDATE` で原子的に加算する。

    PostgreSQL / SQLite 両方の native upsert を使うことで、SAVEPOINT +
    IntegrityError fallback を退役させ、初回並行 INSERT 競合を完全に
    排除する。`updated_at` は ORM の `onupdate` フックをバイパスする
    ため、SET 句で明示的に `func.now()` を渡す。
    """
    dialect_name = db.session.get_bind().dialect.name
    if dialect_name == "postgresql":
        upsert = pg_insert
    elif dialect_name == "sqlite":
        upsert = sqlite_insert
    else:
        # 他 dialect (MySQL 等) は現状非サポート。フォールバックとして
        # 純粋な UPDATE → 行が無ければ INSERT (best-effort) を残す。
        result = db.session.execute(
            update(StorageUsage)
            .where(StorageUsage.user_id == user_id)
            .values(
                used_bytes=StorageUsage.used_bytes + delta,
                updated_at=func.now(),
            )
        )
        if result.rowcount == 0:
            db.session.add(
                StorageUsage(user_id=user_id, used_bytes=delta)
            )
        return

    stmt = upsert(StorageUsage).values(
        user_id=user_id, used_bytes=delta,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={
            "used_bytes": StorageUsage.__table__.c.used_bytes + stmt.excluded.used_bytes,
            "updated_at": func.now(),
        },
    )
    db.session.execute(stmt)


def record_upload(user, size: int, *, suppress_commit: bool = False) -> None:
    """アップロード成功後の使用量加算 (アトミック upsert)。

    `INSERT ... ON CONFLICT (user_id) DO UPDATE SET used_bytes = used_bytes + :size`
    で並行リクエスト下でも加算が消失せず、初回並行 INSERT 競合も発生
    しない (PostgreSQL / SQLite 両対応)。SAVEPOINT + IntegrityError
    fallback は退役。

    Args:
        suppress_commit: True のとき内部で `db.session.commit()` を呼ばない。
            呼出側が Voucher INSERT などを同一トランザクションでまとめる
            ときに使う (`create_voucher_from_upload` 等)。デフォルト False
            は既存呼出側との後方互換。
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    _upsert_storage_usage(user.id, size)
    if not suppress_commit:
        db.session.commit()


def record_delete(user, size: int, *, suppress_commit: bool = False) -> None:
    """削除後の使用量減算 (0 未満にはしない)。

    `CASE WHEN used_bytes >= :size THEN used_bytes - :size ELSE 0 END`
    で SQLite/PostgreSQL の両方で動くアトミック更新。レコードがない
    ケースは整合性異常の可能性があるため warning ログを残す。

    Args:
        suppress_commit: True のとき内部で `db.session.commit()` を呼ばない。
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
    if not suppress_commit:
        db.session.commit()
