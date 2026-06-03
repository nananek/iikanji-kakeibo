"""E6 (#113) §15.4 PR-2: 全データエクスポートのサーバ一時保存ジョブ。

クライアントがブラウザ内で生成した zip を `encryptBackupArchive` (Argon2id +
AES-256-GCM, パスフレーズ方式) で暗号化し、その暗号文 (.ikexport) を storage に
一時保存する。サーバは暗号文を預かるだけで平文 (CSV/画像/backup.json) も
パスフレーズも MK も持たない。

ジョブはアップロード時点で即 `ready` (zip 生成はクライアント側で完了済のため、
§15.4 の generating 状態は使わない)。`expires_at` 経過後はダウンロード不可
(読み取り時 410)。物理削除は `flask export-cleanup` (PR-3) が行う。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, Integer

from app.extensions import db


# bigserial PK は SQLite では autoincrement しないため、テスト (SQLite) では
# INTEGER PRIMARY KEY にフォールバックさせる (app/models/audit.py と同方針)。
_BigIntPK = BigInteger().with_variant(Integer, "sqlite")


# DL リンクの有効期間 (§15.4 = 24 時間)。expires_at の既定計算に使う。
EXPORT_TTL = timedelta(hours=24)

STATUS_READY = "ready"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"


def _default_expires_at():
    return datetime.now(timezone.utc) + EXPORT_TTL


class ExportJob(db.Model):
    """サーバ一時保存された暗号化エクスポート (§15.4)。"""

    __tablename__ = "export_jobs"

    id = db.Column(_BigIntPK, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # ready / expired / failed (text で将来拡張可)
    status = db.Column(db.String(20), nullable=False, default=STATUS_READY)
    # storage バックエンド上のキー (exports/{user_id}/{job_id}.ikexport)
    storage_key = db.Column(db.String(255), nullable=False)
    # 暗号化済 blob のバイト数 (UI 表示用)
    file_size = db.Column(db.BigInteger, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # アップロード完了時刻 (クライアント生成のため作成時に設定)
    ready_at = db.Column(db.DateTime(timezone=True), nullable=True)
    # created_at + 24h。`flask export-cleanup` (PR-3) が削除に使う。
    expires_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=_default_expires_at
    )
    download_count = db.Column(db.SmallInteger, nullable=False, default=0)

    user = db.relationship("User")

    __table_args__ = (
        db.Index("ix_export_jobs_user_expires", "user_id", "expires_at"),
    )

    def is_expired(self, now=None):
        """expires_at を過ぎていれば True。

        DB の timezone-aware/naive 差異を吸収するため、tzinfo が無い値は
        UTC とみなして比較する。
        """
        if now is None:
            now = datetime.now(timezone.utc)
        exp = self.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return exp is not None and exp < now

    def __repr__(self):
        return f"<ExportJob id={self.id} user={self.user_id} status={self.status}>"
