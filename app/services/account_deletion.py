"""退会フロー: ユーザーの全データを削除する (Phase 4 公開運用整備)。

電帳法スキャナ保存の 7 年保管義務に従い `VoucherAuditLog` は
`user_id` / `voucher_id` を NULL 化して匿名で保持し、他は物理削除する。

呼び出し側は `delete_user_account(user_id)` を呼ぶだけでよい。本関数は
内部で `db.session.commit()` を行うため、呼出元の未 commit な変更は
含めないこと (呼出順序の予測可能性を保つため)。
"""

from flask import current_app
from sqlalchemy import select as sa_select, update

from app.extensions import db
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft
from app.models.ai_usage_log import AIUsageLog
from app.models.api_key import APIKey
from app.models.audit import AuditGrant, AuditGrantAccount
from app.models.auto_import import WebhookConfig
from app.models.balance_cache import BalanceCache
from app.models.csv_column_profile import CsvColumnProfile
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.medical import MedicalExpense
from app.models.oauth import OAuthDevice, OAuthToken
from app.models.storage import StorageUsage
from app.models.account import Account
from app.models.tax_form import TaxFormMapping
from app.models.user import User
from app.models.voucher import Voucher
from app.models.voucher_audit_log import VoucherAuditLog
from app.models.webauthn import WebAuthnCredential
from app.services.storage import get_storage_backend, make_thumbnail_key


def delete_user_account(user_id: int) -> None:
    """ユーザーの全データを削除する。

    電帳法保管対象 (`VoucherAuditLog`) は user_id / voucher_id を NULL
    化して匿名化保持。他は物理削除。

    削除順序は SQLAlchemy session の autoflush + FK 制約に対応:
    1. VoucherAuditLog の user_id を NULL 化 (logical delete log を残す)
    2. Voucher のストレージファイルを削除 (best-effort) → DB row 物理削除
       (`ondelete=SET NULL` で AuditLog の voucher_id は自動 NULL 化)
    3. AIDraft + ストレージファイル
    4. JournalEntryLine → JournalEntry
    5. その他 user_id を持つテーブル全削除
    6. AuditGrant (owner_user_id / auditor_user_id どちらでも該当)
    7. User 物理削除

    各削除後に flush して FK 制約違反の早期検出を狙う。最後に commit。
    """
    user = db.session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")

    backend = get_storage_backend()

    # 1. VoucherAuditLog の user_id を NULL 化 (匿名化保持)
    db.session.execute(
        update(VoucherAuditLog)
        .where(VoucherAuditLog.user_id == user_id)
        .values(user_id=None)
    )
    db.session.flush()

    # 2. Voucher: ストレージファイルを先に削除 → DB row 物理削除
    # (ondelete=SET NULL で VoucherAuditLog.voucher_id は自動 NULL 化)
    vouchers = Voucher.query.filter_by(user_id=user_id).all()
    for v in vouchers:
        for k in (v.image_key, make_thumbnail_key(v.image_key)):
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "account_deletion: voucher storage delete failed %s: %s",
                    k, e,
                )
        db.session.delete(v)
    db.session.flush()

    # 3. AIDraft + ストレージファイル
    drafts = AIDraft.query.filter_by(user_id=user_id).all()
    for d in drafts:
        for k in (d.image_key, make_thumbnail_key(d.image_key)):
            try:
                backend.delete(k)
            except Exception as e:
                current_app.logger.warning(
                    "account_deletion: draft storage delete failed %s: %s",
                    k, e,
                )
        db.session.delete(d)
    db.session.flush()

    # 4. JournalEntryLine を JournalEntry より先に削除する。
    # `JournalEntryLine.journal_entry_id` の FK は `ondelete=CASCADE` 未指定
    # のため、`.query.delete()` のバルク DELETE は ORM relationship cascade を
    # 発火させず、PostgreSQL では FK 制約違反になる (SQLite は FK 弱で発覚せず)。
    # 順序: ① journal_entry_id 経由 → ② account_user_id 経由 (独立カラム、
    # 他ユーザー仕訳に紐づく自分の account 行も念のため削除) → ③ JournalEntry
    entry_ids_subq = sa_select(JournalEntry.id).where(
        JournalEntry.user_id == user_id
    )
    db.session.execute(
        JournalEntryLine.__table__.delete().where(
            JournalEntryLine.journal_entry_id.in_(entry_ids_subq)
        )
    )
    db.session.flush()
    # NOTE: JournalEntryLine.account_user_id == user_id の二段目 DELETE は
    # 旧実装で「相互参照のクリーンアップ」目的で行っていたが、コードベース
    # 全体で `account_user_id` は親 `JournalEntry.user_id` と常に同値で
    # 設定されており (accounting.py / journal.py / api.py すべて)、クロス
    # ユーザー参照は設計上発生しない。上の subquery DELETE で全削除済の
    # ため二段目は dead code。PR #97 review Medium 指摘で除去。
    # JournalEntry 本体を削除
    JournalEntry.query.filter_by(user_id=user_id).delete()
    db.session.flush()

    # 5. user_id を持つ各テーブルを削除
    for model in (
        UserAIConfig, AIUsageLog, APIKey,
        BalanceCache, CsvColumnProfile,
        FiscalClose, MedicalExpense,
        OAuthDevice, OAuthToken,
        StorageUsage, Account, WebAuthnCredential,
        WebhookConfig,
    ):
        model.query.filter_by(user_id=user_id).delete()

    # TaxFormMapping (user_id は ForeignKey なし、独立カラム)
    db.session.execute(
        TaxFormMapping.__table__.delete().where(
            TaxFormMapping.user_id == user_id
        )
    )

    # 6. AuditGrant (owner_user_id / auditor_user_id どちらでも該当)
    grants = AuditGrant.query.filter(
        (AuditGrant.owner_user_id == user_id)
        | (AuditGrant.auditor_user_id == user_id)
    ).all()
    for grant in grants:
        AuditGrantAccount.query.filter_by(audit_grant_id=grant.id).delete()
        db.session.delete(grant)
    db.session.flush()

    # 7. User 物理削除
    db.session.delete(user)
    db.session.commit()
