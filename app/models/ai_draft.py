from datetime import datetime, timezone

from app.extensions import db


class AIDraft(db.Model):
    """AI証憑の一時保存"""

    __tablename__ = "ai_drafts"
    # E5 (#111): aad_id は下書き単位で一意 (E2EE 化された下書き画像の AAD 束縛)。
    # レガシー平文下書きは NULL (Postgres は NULL を distinct 扱いするため併存可)。
    # 下書き → 証憑移行時に Voucher.aad_id へそのまま引き継ぐ (再暗号化不要)。
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "aad_id", name="uq_ai_drafts_user_aad_id"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    image_key = db.Column(db.String(255), nullable=False)
    image_mime = db.Column(db.String(50), nullable=False)
    # SHA-256。E5 (#111) E2EE 下書きでは暗号文画像のハッシュ (= file_hash_cipher
    # 相当)。サーバが MK なしで暗号文の改ざんを検証できる電帳法 Q11 の cipher 側。
    file_hash = db.Column(db.String(64), nullable=True)  # SHA-256
    # E5 (#111): 下書き画像の E2EE 化。いずれも dual-write 期 (059) は NULL 許容。
    # クライアントが filename + image_mime 等を JSON 化し AES-GCM 暗号化
    # (AAD = "vmeta" + user_id + aad_id、voucher と同ドメイン) した blob と 12B IV。
    encrypted_meta_blob = db.Column(db.LargeBinary, nullable=True)
    meta_iv = db.Column(db.LargeBinary, nullable=True)
    # SHA-256(平文画像)。クライアントが計算して送信 (電帳法 Q11 の平文側)。
    file_hash_plain = db.Column(db.String(64), nullable=True)
    # クライアント生成サムネイル (暗号文) のストレージキー。
    thumbnail_key = db.Column(db.String(255), nullable=True)
    # E5 (#111): クライアント復号の AAD に束縛する安定識別子。サーバが init で
    # 生成する 63bit ランダム。下書き → 証憑移行時に Voucher.aad_id へ引き継ぐため、
    # 暗号化済み画像/サムネ/meta を再暗号化せずそのまま証憑へ移せる。E2EE 下書きの
    # みセット (レガシー平文下書きは NULL)。
    aad_id = db.Column(db.BigInteger, nullable=True)
    # 容量計上 (Phase 5 #70): AIDraft 生成時のバイト数。Voucher 化時に
    # 同値を Voucher.file_size に引き継ぎ、計上は不変 (所有権移転)。
    # Voucher 化されずに reject/期限切れで削除された場合は file_size を
    # 元に record_delete する。NULL は Phase 5 計上開始前のレガシー。
    file_size = db.Column(db.BigInteger, nullable=True)
    comment = db.Column(db.String(500), default="")
    suggestions_json = db.Column(db.Text, nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default="pending"
    )  # pending (画像のみ、未解析) / temp (UI 解析直後、未保存) /
       # analyzed (suggestions 保存済) / done (仕訳登録済)
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", backref=db.backref("ai_drafts", lazy="dynamic"))
