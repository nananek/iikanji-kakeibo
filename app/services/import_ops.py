"""CSV / OFX / Web 取込で共通の「確認画面 → 一括仕訳化」ロジック。

取込確認画面 (``csv_import`` / ``ofx_import`` / ``web_import``) の confirm
ステップと、MCP サーバーの ``import_csv`` ツールは、いずれもこの
:func:`import_rows` を通して仕訳を生成する。ビュー側でロジックを重複実装すると
「フロントと MCP で取込結果がズレる」ため、必ずこの関数を共有すること。

バリデーションは「スキップ」で表現する (例外を投げずに件数へ集計) のが
確認画面の従来挙動であり、MCP からも同じ結果を返す。
"""

import uuid
from datetime import date as date_type

from app.extensions import db
from app.models.account import Account, AccountType
from app.services.accounting import create_cashbook_entry, create_transfer_entry
from app.services.audit import get_submitted_account_codes
from app.services.clock import today as app_today
from app.services.fiscal import (
    check_period_open_for_new,
    get_capital_account_code,
    get_restricted_before_year,
    is_year_open,
)


def import_rows(user_id, payment_account_code, rows, *, source="csv",
                old_year_action="skip", batch_id=None):
    """取込確認画面で選択された行を一括で仕訳化する。

    Args:
        user_id: 対象ユーザー。
        payment_account_code: 支払元/入金先の資産・負債科目コード。
        rows: list of dict。各要素は
            ``enabled`` (省略時 True) / ``date`` (ISO 文字列, 必須) /
            ``description`` / ``deposit`` / ``withdrawal`` / ``category_code``。
        source: 仕訳の source (``csv`` / ``ofx`` / ``web`` など)。
        old_year_action: ``skip`` (既定) または ``capital``
            (未開設年度を当年の元入金仕訳に変換)。
        batch_id: 既存バッチに紐付ける場合に指定 (省略時は新規 UUID)。

    Returns:
        dict: ``imported`` / ``skipped`` / ``capital_count`` / ``batch_id``。
    """
    asset_type = AccountType.query.filter_by(code="asset").first()
    liability_type = AccountType.query.filter_by(code="liability").first()
    transfer_type_ids = set()
    if asset_type:
        transfer_type_ids.add(asset_type.id)
    if liability_type:
        transfer_type_ids.add(liability_type.id)

    locked_codes = get_submitted_account_codes(user_id)
    restricted_before = get_restricted_before_year(user_id)
    capital_code = get_capital_account_code(user_id)
    today_year = app_today().year

    if batch_id is None:
        batch_id = str(uuid.uuid4())

    imported = 0
    skipped = 0
    capital_count = 0

    for row in rows or []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        if not row.get("enabled", True):
            skipped += 1
            continue

        row_date_str = row.get("date")
        if not row_date_str:
            skipped += 1
            continue

        try:
            row_date = date_type.fromisoformat(str(row_date_str))
        except (ValueError, TypeError):
            skipped += 1
            continue

        description = row.get("description") or ""
        try:
            deposit = int(row.get("deposit", 0) or 0)
            withdrawal = int(row.get("withdrawal", 0) or 0)
        except (ValueError, TypeError):
            skipped += 1
            continue
        category_code = row.get("category_code") or ""

        if not category_code or (deposit == 0 and withdrawal == 0):
            skipped += 1
            continue

        # 提出済みロック科目チェック
        if locked_codes and {payment_account_code, category_code} & locked_codes:
            skipped += 1
            continue

        # 未開設年度チェック
        if restricted_before and row_date.year < restricted_before:
            if not is_year_open(user_id, row_date.year):
                if old_year_action == "capital" and capital_code:
                    description = f"({row_date_str}) {description}"
                    row_date = date_type(today_year, 1, 1)
                    category_code = capital_code
                    capital_count += 1
                else:
                    skipped += 1
                    continue

        # 確定済み期間チェック
        err = check_period_open_for_new(user_id, row_date.year, row_date.month)
        if err:
            skipped += 1
            continue

        cat_account = Account.query.filter_by(
            user_id=user_id, code=category_code
        ).first()
        is_transfer = bool(
            cat_account and cat_account.account_type_id in transfer_type_ids
        )

        if is_transfer:
            amount = deposit or withdrawal
            if withdrawal > 0:
                create_transfer_entry(
                    user_id=user_id,
                    date=row_date,
                    from_account_code=payment_account_code,
                    to_account_code=category_code,
                    amount=amount,
                    description=description,
                    batch_id=batch_id,
                    source=source,
                )
            else:
                create_transfer_entry(
                    user_id=user_id,
                    date=row_date,
                    from_account_code=category_code,
                    to_account_code=payment_account_code,
                    amount=amount,
                    description=description,
                    batch_id=batch_id,
                    source=source,
                )
            imported += 1
        elif deposit > 0:
            create_cashbook_entry(
                user_id=user_id,
                date=row_date,
                transaction_type="income",
                payment_account_code=payment_account_code,
                category_account_code=category_code,
                amount=deposit,
                description=description,
                batch_id=batch_id,
                source=source,
            )
            imported += 1
        elif withdrawal > 0:
            create_cashbook_entry(
                user_id=user_id,
                date=row_date,
                transaction_type="expense",
                payment_account_code=payment_account_code,
                category_account_code=category_code,
                amount=withdrawal,
                description=description,
                batch_id=batch_id,
                source=source,
            )
            imported += 1
        else:
            skipped += 1

    # create_* は個別に commit するが、念のため残りを確定させる。
    db.session.commit()

    return {
        "imported": imported,
        "skipped": skipped,
        "capital_count": capital_count,
        "batch_id": batch_id,
    }
