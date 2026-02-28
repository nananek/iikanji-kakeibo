"""証憑ヘルパー — AIDraft から Voucher への移行"""

from app.extensions import db
from app.models.voucher import Voucher
from app.models.ai_draft import AIDraft


def create_voucher_from_draft(draft: AIDraft, journal_entry_id: int) -> Voucher:
    """AIDraft から Voucher を作成し、ドラフトを削除する。

    画像ファイルはストレージに残し、Voucher が参照を引き継ぐ。
    呼び出し元で db.session.commit() すること。
    """
    voucher = Voucher(
        user_id=draft.user_id,
        journal_entry_id=journal_entry_id,
        image_key=draft.image_key,
        image_mime=draft.image_mime,
        file_hash=draft.file_hash,
        uploaded_at=draft.created_at,
    )
    db.session.add(voucher)
    db.session.delete(draft)
    return voucher
