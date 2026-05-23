"""ユーザー別AI API設定"""

from datetime import datetime, timezone

from app.extensions import db


class UserAIConfig(db.Model):
    """ユーザーごとの AI API 設定。

    E2EE 移行 (E2 Phase E2-a):
        v4.x までは `api_key_encrypted` (Fernet サーバ暗号化) のみ。
        v5.0 では `api_key_blob` (AES-256-GCM 暗号文) + `api_key_iv` を
        **クライアント側で MK で暗号化** して保管 (サーバは復号不可)。

    移行期間中は両カラムが共存し、`migrated_at` で 1 回限りの migrate-key
    呼出を制御する (一度移行したら api_key_encrypted は NULL クリア)。
    Phase E2-b (別マイグレーション) で旧カラム削除。

    設計書 §11 参照。
    """

    __tablename__ = "user_ai_configs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True
    )
    provider = db.Column(db.String(20), nullable=False, default="openai")
    # 旧: Fernet サーバ暗号化された API キー。移行完了後 NULL クリア
    # (Phase E2-b で DROP 予定)。
    api_key_encrypted = db.Column(db.LargeBinary, nullable=True)
    # 新: クライアント側 MK で AES-256-GCM 暗号化された API キー (タグ込)
    api_key_blob = db.Column(db.LargeBinary, nullable=True)
    # AES-GCM IV (12B)。blob と一緒に保存
    api_key_iv = db.Column(db.LargeBinary, nullable=True)
    # E2EE 形式への移行完了時刻 (migrate-key endpoint 1 回限り判定用)
    migrated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    model_name = db.Column(db.String(100), nullable=False, default="")
    custom_prompt = db.Column(db.Text, nullable=False, default="")
    compliance_check = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship(
        "User", backref=db.backref("ai_config", uselist=False, lazy="select")
    )

    @property
    def is_e2ee(self) -> bool:
        """E2EE 形式 (api_key_blob) で保管されているか判定する。"""
        return self.api_key_blob is not None and self.api_key_iv is not None

    def __repr__(self):
        return (
            f"<UserAIConfig user={self.user_id} provider={self.provider} "
            f"e2ee={self.is_e2ee}>"
        )
