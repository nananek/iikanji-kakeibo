from datetime import datetime, timezone

from app.extensions import db


class WebAuthnCredential(db.Model):
    """Passkey (WebAuthn) クレデンシャル"""

    __tablename__ = "webauthn_credentials"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    credential_id = db.Column(db.LargeBinary, nullable=False, unique=True)
    credential_public_key = db.Column(db.LargeBinary, nullable=False)
    current_sign_count = db.Column(db.Integer, nullable=False, default=0)
    name = db.Column(db.String(100), nullable=False, default="")
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    last_used_at = db.Column(db.DateTime, nullable=True)
    transports = db.Column(db.String(255), nullable=True)

    user = db.relationship(
        "User", backref=db.backref("webauthn_credentials", lazy="dynamic")
    )

    def __repr__(self):
        return f"<WebAuthnCredential {self.id} user={self.user_id} name={self.name!r}>"
