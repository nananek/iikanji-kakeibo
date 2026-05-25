"""ユーザー別AI API設定"""

from datetime import datetime, timezone

from app.extensions import db


class UserAIConfig(db.Model):
    """ユーザーごとの AI API 設定。

    E2EE 化完了 (Phase E2-b):
        API キーはクライアント側で MK で AES-256-GCM 暗号化された状態
        (api_key_blob + api_key_iv) で保管され、サーバは復号できない。
        旧 Fernet サーバ暗号化カラム (api_key_encrypted) + migrate-key
        endpoint 1 回限り判定用カラム (migrated_at) は Phase E2-b で削除済。

    設計書 §11 参照。
    """

    __tablename__ = "user_ai_configs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True
    )
    provider = db.Column(db.String(20), nullable=False, default="openai")
    # クライアント側 MK で AES-256-GCM 暗号化された API キー (タグ込)
    api_key_blob = db.Column(db.LargeBinary, nullable=True)
    # AES-GCM IV (12B)。blob と一緒に保存
    api_key_iv = db.Column(db.LargeBinary, nullable=True)
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
