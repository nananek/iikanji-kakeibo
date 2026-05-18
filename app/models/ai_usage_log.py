"""外部 AI API 呼び出し履歴

ユーザーが自分のプロバイダーコンソール (OpenAI / Anthropic / Google) で
確認できる利用量と突合できるよう、サーバー側からの呼び出し記録を残す。
プライバシー最小化: プロンプト本文・レスポンス本文・API キー・画像本体は
保存しない。トークン数とメタデータのみ。
"""

from datetime import datetime, timezone

from app.extensions import db


class AIUsageLog(db.Model):
    """外部 AI API 呼び出し 1 件ごとの記録"""

    __tablename__ = "ai_usage_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    provider = db.Column(db.String(20), nullable=False)
    model = db.Column(db.String(100), nullable=False)
    # 機能識別子 (receipt_round1 / receipt_round2 / voucher_attach /
    # web_extract / csv_columns_detect / csv_reconcile_ai / category_suggest)
    feature = db.Column(db.String(40), nullable=False)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    # ok / http_error / timeout / parse_error / other_error
    status = db.Column(db.String(20), nullable=False, default="ok")
    http_status = db.Column(db.Integer, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    __table_args__ = (
        db.Index(
            "ix_ai_usage_logs_user_created",
            "user_id", "created_at",
        ),
    )

    user = db.relationship(
        "User", backref=db.backref("ai_usage_logs", lazy="dynamic")
    )

    def __repr__(self):
        return (
            f"<AIUsageLog id={self.id} user={self.user_id} "
            f"provider={self.provider} feature={self.feature}>"
        )
