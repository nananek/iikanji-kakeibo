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
from app.services.mail import send_email


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
            ときに使う (`finalize_voucher_upload` 等)。デフォルト False
            は既存呼出側との後方互換。
    """
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    _upsert_storage_usage(user.id, size)
    if not suppress_commit:
        db.session.commit()


# quota_warning メール通知の閾値 (Phase 6 #71)。
# 80% / 95% 到達時に通知、70% 未満まで回復したらリセット (ヒステリシス)。
_QUOTA_WARNING_THRESHOLD = 80.0
_QUOTA_CRITICAL_THRESHOLD = 95.0
_QUOTA_RESET_THRESHOLD = 70.0


def maybe_send_quota_warning(user) -> None:
    """ストレージ使用率が新しい閾値帯に達したら quota_warning メールを送信。

    閾値到達検出ロジック (`User.last_quota_warning_level` を状態として保持):
    - <70%: NULL 化 (次回再通知できるよう状態リセット)
    - 70-80%: 状態維持 (何もしない、ヒステリシス区間)
    - 80-95%: 直近が NULL or "warning" 以外なら "warning" メール送信
    - >=95%: 直近が "critical" 以外なら "critical" メール送信

    呼出側は `record_upload` 完了後 (= 容量加算 + commit 後) に呼ぶこと。
    `send_email` の失敗はログに残して握る (ユーザー操作を妨げない)。

    可逆性に注意: critical → warning への戻り通知は意図的にしない (運用上
    「容量逼迫した」事実だけ伝えれば十分で、戻り通知はノイズになる)。
    """
    from flask import current_app, url_for

    try:
        used = get_used_bytes(user)
        quota = get_quota_bytes(user)
        if quota <= 0:
            return
        pct = (used / quota) * 100
        prev_level = user.last_quota_warning_level

        # リセット判定 (70% 未満まで回復)
        if pct < _QUOTA_RESET_THRESHOLD:
            if prev_level is not None:
                user.last_quota_warning_level = None
                db.session.commit()
            return

        # ヒステリシス区間 (70-80%): 状態維持
        if pct < _QUOTA_WARNING_THRESHOLD:
            return

        # 新しい閾値レベルを判定
        new_level = "critical" if pct >= _QUOTA_CRITICAL_THRESHOLD else "warning"

        # 既に同じ or それ以上のレベルで通知済なら再送しない
        # (critical 中に warning は通知しない、warning 中に warning も再送しない)
        if prev_level == new_level:
            return
        if prev_level == "critical" and new_level == "warning":
            # critical → warning への戻りは通知しない
            return

        # メール送信 (失敗時は state を巻き戻して次回再送できるようにする)
        user.last_quota_warning_level = new_level
        db.session.commit()

        try:
            settings_url = url_for("settings.index", _external=True)
        except Exception:
            settings_url = "/settings/"

        send_email(user.email, "quota_warning", {
            "username": user.username,
            "percentage": round(pct, 1),
            "used_mb": round(used / (1024 * 1024), 1),
            "quota_mb": round(quota / (1024 * 1024), 1),
            "level": new_level,
            "settings_url": settings_url,
        })
    except Exception as e:
        current_app.logger.exception(
            "maybe_send_quota_warning failed (user=%d): %s",
            getattr(user, "id", -1), e,
        )


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
