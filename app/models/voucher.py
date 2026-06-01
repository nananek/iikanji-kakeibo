from datetime import datetime, timezone

from app.extensions import db


class Voucher(db.Model):
    """証憑（電帳法対応の永続保存）"""

    __tablename__ = "vouchers"
    # E4 (#111) Option C: aad_id は voucher 単位で一意 (voucher 間の ciphertext
    # swap を AAD で検知する swap 防止)。レガシー平文証憑は NULL (Postgres は
    # NULL を distinct 扱いするため併存可)。
    __table_args__ = (
        db.UniqueConstraint("user_id", "aad_id", name="uq_vouchers_user_aad_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    journal_entry_id = db.Column(
        db.Integer,
        db.ForeignKey("journal_entries.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    image_key = db.Column(db.String(255), nullable=False)
    # E5 PR-5 (#111): image_mime 列は 060 で DROP 済。E2EE 証憑の実 MIME は
    # encrypted_meta_blob 内、レガシー平文証憑は octet-stream + content-sniff で
    # 配信するため列は不要になった。
    # SHA-256。E4 (#111) では暗号文画像のハッシュ (= file_hash_cipher 相当) を
    # 保持する。サーバが MK なしで「あるはずの暗号文が改ざんされていないか」を
    # 検証できる電帳法 Q11 ハイブリッドの cipher 側。平文側は file_hash_plain。
    file_hash = db.Column(db.String(64), nullable=True)
    # E4 (#111): 証憑の E2EE 化。いずれも dual-write 期 (056) は NULL 許容。
    # クライアントが filename + image_mime 等を JSON 化し AES-GCM 暗号化
    # (AAD = "vmeta" + user_id + voucher_id) した blob と 12B IV。平文
    # original_filename 列は 057 (PR-F) で DROP 済 (暗号化証憑では本 blob 内)。
    encrypted_meta_blob = db.Column(db.LargeBinary, nullable=True)
    meta_iv = db.Column(db.LargeBinary, nullable=True)
    # SHA-256(平文画像)。クライアントが計算して送信。復号後に再計算して改ざん
    # 検出する (電帳法 Q11 ハイブリッドの平文側)。
    file_hash_plain = db.Column(db.String(64), nullable=True)
    # クライアント生成サムネイル (暗号文) のストレージキー。サーバ Pillow 生成
    # (_thumb.jpg サフィックス) は E4 後半で廃止し、本列ベースに統一する。
    thumbnail_key = db.Column(db.String(255), nullable=True)
    # E4 (#111) Option C: クライアント復号の AAD に束縛する安定識別子。サーバが
    # init で生成する 63bit ランダム (voucher_id とは独立)。backup/restore で PK
    # が再採番されても本値は保持されるため、AAD を voucher_id ではなく本値に
    # 束縛することで復元後もクライアント復号が可能。E2EE 証憑のみセット
    # (レガシー平文証憑は NULL)。
    aad_id = db.Column(db.BigInteger, nullable=True)
    # 容量計上 (Phase 5 #70) のためのファイルサイズ (バイト)。新規作成時に
    # セット。既存 Voucher は NULL のままで、整合性監査バッチで埋める想定。
    file_size = db.Column(db.BigInteger, nullable=True)
    uploaded_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 論理削除 (Phase 5 #70 / 電帳法証跡永続化): 削除時にタイムスタンプを
    # セットし、Voucher row 自体は DB に残す。これにより `VoucherAuditLog`
    # の `action="deleted"` を FK RESTRICT 下でも永続化できる。
    # 一覧・検索・REST API は `Voucher.active()` (= deleted_at IS NULL)
    # で透過的にフィルタする。
    deleted_at = db.Column(
        db.DateTime(timezone=True), nullable=True, index=True,
    )

    user = db.relationship("User", backref=db.backref("vouchers", lazy="dynamic"))
    # journal_entry.vouchers backref は削除済も含む全件を返す。UI/API では
    # `entry.active_vouchers` プロパティ (JournalEntry 側で定義) を使い、
    # 全件 (削除済含む) が必要な log_voucher_orphan / api_voucher_logs
    # などでは `entry.vouchers` を直接使うこと。
    journal_entry = db.relationship(
        "JournalEntry", backref=db.backref("vouchers", lazy="select")
    )

    @classmethod
    def active(cls):
        """論理削除されていない Voucher のみを返す query."""
        return cls.query.filter(cls.deleted_at.is_(None))

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None
