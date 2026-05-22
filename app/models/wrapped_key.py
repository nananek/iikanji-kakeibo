"""E2EE Master Key の暗号化ラップ (wrapped_keys テーブル)。

設計書 §10.1 参照。Master Key は 3 つの認証要素 (Passkey PRF / passphrase /
recovery_seed) でそれぞれラップしてサーバに保管する。サーバは平文 MK を
持たないので、いずれかの要素を持つクライアントだけが MK を復元できる。
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Index

from app.extensions import db


METHOD_PASSKEY_PRF = "passkey_prf"
METHOD_PASSPHRASE = "passphrase"
METHOD_RECOVERY_SEED = "recovery_seed"
ALLOWED_METHODS = (METHOD_PASSKEY_PRF, METHOD_PASSPHRASE, METHOD_RECOVERY_SEED)


class WrappedKey(db.Model):
    """ラップ済 Master Key (Worker クロージャに展開する前の保管形式)。"""

    __tablename__ = "wrapped_keys"
    __table_args__ = (
        CheckConstraint(
            "method IN ('passkey_prf', 'passphrase', 'recovery_seed')",
            name="ck_wrapped_keys_method",
        ),
        # passkey_prf: (user_id, method, credential_id) 単位で UNIQUE
        Index(
            "uq_wrapped_keys_passkey",
            "user_id", "method", "webauthn_credential_id",
            unique=True,
            sqlite_where=db.text("webauthn_credential_id IS NOT NULL"),
            postgresql_where=db.text("webauthn_credential_id IS NOT NULL"),
        ),
        # passphrase / recovery_seed: (user_id, method) 単位で UNIQUE
        # (PostgreSQL NULL ≠ NULL 仕様への対応、設計書 §10.1)
        Index(
            "uq_wrapped_keys_passphrase_recovery",
            "user_id", "method",
            unique=True,
            sqlite_where=db.text("webauthn_credential_id IS NULL"),
            postgresql_where=db.text("webauthn_credential_id IS NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    method = db.Column(db.String(20), nullable=False)
    # method=passkey_prf 時のみ非 NULL。webauthn_credentials.id (PK) への FK。
    webauthn_credential_id = db.Column(
        db.Integer,
        db.ForeignKey("webauthn_credentials.id", ondelete="CASCADE"),
        nullable=True,
    )
    wrapped_master_key = db.Column(db.LargeBinary, nullable=False)
    # AES-GCM IV (12B)、ciphertext とは別カラムで保管 (設計書 §10.1 注記)。
    wrap_iv = db.Column(db.LargeBinary, nullable=False)
    # method=passphrase 時の Argon2id salt (16B)。recovery_seed では NULL。
    salt = db.Column(db.LargeBinary, nullable=True)
    # method=passphrase 時の Argon2id パラメータ {memory, iterations, parallelism}。
    kdf_params = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # アンラップ成功時に更新 (WebAuthn 認証成功とは別)。
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    label = db.Column(db.String(100), nullable=True)

    # passive_deletes=True で DB-level CASCADE に任せる (ondelete="CASCADE")
    user = db.relationship(
        "User",
        backref=db.backref(
            "wrapped_keys", lazy="dynamic", passive_deletes=True
        ),
    )
    webauthn_credential = db.relationship("WebAuthnCredential")

    def __repr__(self):
        return f"<WrappedKey id={self.id} user={self.user_id} method={self.method}>"
