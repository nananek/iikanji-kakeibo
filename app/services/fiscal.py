"""月次確定・決算期間管理サービス"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, or_

from app.extensions import db
from app.models.account import Account, AccountType
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry, JournalEntryLine
from app.models.user import User
from app.services.accounting import get_next_entry_number


# closing 仕訳のセンチネル blob_iv 長 (AES-GCM IV と同じ 12B)。
_CLOSING_IV_LEN = 12

# 期間番号 → 表示名
PERIOD_LABELS = {
    0: "期首振戻月",
    1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
    7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月",
    13: "決算月1", 14: "決算月2", 15: "決算月3",
    16: "損益振替",
}


def period_condition(year, p):
    """単一期間のSQLAlchemy条件を返す（共通）

    E3-F (PR-D-6-2): fiscal_month / fiscal_year ベースで判定する。マイグレ 054
    で全行の fiscal_month が backfill 済 (旧 fiscal_period 明示分はその値、未指定
    の通常仕訳は date.month) のため、旧来の「fiscal_period が NULL なら date の月で
    救済」分岐は不要になった。平文 date 列は後続 PR (D-6-5) で DROP する。
    """
    return and_(
        JournalEntry.fiscal_year == year,
        JournalEntry.fiscal_month == p,
    )


def period_range_filter(year, pf, pt):
    """期間範囲のOR条件を返す"""
    conds = [period_condition(year, p) for p in range(pf, pt + 1)]
    return or_(*conds) if conds else None


def adjust_date_for_fiscal_period(entry_date, fiscal_period):
    """特殊期間に応じて日付を補正する。期首振戻→1/1、決算月→12/31"""
    if fiscal_period is None:
        return entry_date
    year = entry_date.year
    if fiscal_period == 0:
        return date(year, 1, 1)
    if fiscal_period in (13, 14, 15, 16):
        return date(year, 12, 31)
    return entry_date


def get_effective_period(entry):
    """仕訳の実効期間を返す

    E3-F PR-D-6-5-pre1: fiscal_month を使用 (マイグレ 054 で全行 backfill 済、
    全 WRITE 経路も populate 済)。旧 fiscal_period / date.month フォールバックは
    撤去 (date / fiscal_period 列は D-6-5 で DROP)。
    """
    return entry.fiscal_month


def get_closed_period(user_id, year):
    """指定年度の確定済み期間を返す（-1 = 未確定）"""
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    return fc.closed_period if fc else -1


def get_closed_periods_map(user_id):
    """全確定済み年度の期間マップ {year: closed_period} を返す"""
    rows = FiscalClose.query.filter(
        FiscalClose.user_id == user_id,
        FiscalClose.closed_period >= 0,
    ).all()
    return {fc.year: fc.closed_period for fc in rows}


def get_last_closed(user_id):
    """確定済みの最後の年+期間を返す。未確定なら None"""
    fc = (
        FiscalClose.query
        .filter(FiscalClose.user_id == user_id, FiscalClose.closed_period >= 0)
        .order_by(FiscalClose.year.desc())
        .first()
    )
    if not fc:
        return None
    return {"year": fc.year, "period": fc.closed_period}


def get_closed_periods_for_dates(user_id, dates):
    """日付リストに含まれる年度の確定済み期間を辞書で返す {year: closed_period}"""
    years = set()
    for d in dates:
        if d:
            try:
                years.add(int(d[:4]))
            except (ValueError, TypeError):
                pass
    result = {}
    for y in years:
        cp = get_closed_period(user_id, y)
        if cp >= 0:
            result[y] = cp
    return result


def is_period_locked(user_id, year, period):
    """指定期間がロック済みか判定"""
    return period <= get_closed_period(user_id, year)


def check_entry_modifiable(user_id, entry):
    """仕訳が変更可能か判定。不可ならエラーメッセージを返す"""
    if entry.is_closing:
        return "損益振替仕訳（自動生成）は変更できません。"
    # E3-F PR-D-6-5-pre1: fiscal_year を使用 (全 WRITE 経路で populate 済)。
    # 旧 date.year フォールバックは撤去 (date 列は D-6-5 で DROP)。
    year = entry.fiscal_year
    period = get_effective_period(entry)
    if is_period_locked(user_id, year, period):
        label = PERIOD_LABELS.get(period, f"{period}月")
        return f"{year}年{label}は確定済みのため変更できません。"
    return None


def check_period_open_for_new(user_id, year, period):
    """新規仕訳の対象期間がオープンか判定"""
    if not is_year_open(user_id, year):
        return f"{year}年度は開設されていません。月次確定画面で年度を追加してください。"
    if is_period_locked(user_id, year, period):
        label = PERIOD_LABELS.get(period, f"{period}月")
        return f"{year}年{label}は確定済みのため仕訳を追加できません。"
    return None


def is_year_open(user_id, year):
    """年度が仕訳入力可能か判定。前年以降は常にTrue、前々年以前はFiscalCloseレコード要"""
    user = User.query.get(user_id)
    if not user:
        return False
    created_year = user.created_at.year
    if year >= created_year - 1:
        return True
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    return fc is not None


def get_restricted_before_year(user_id):
    """制限対象となる年度の境界を返す（この年より前が制限対象）"""
    user = User.query.get(user_id)
    if not user:
        return None
    return user.created_at.year - 1


def get_capital_account_code(user_id):
    """元入金科目のコードを返す"""
    account = Account.query.filter_by(
        user_id=user_id, system_role="capital"
    ).first()
    return account.code if account else None


def close_period(user_id, year, period):
    """月次確定を実行。成功時はNone、エラー時はメッセージを返す。

    Phase E3-F-6 で旧 balance_caches テーブル更新は撤去 (BCB に統合)。
    BCB の sync はクライアント側 `bcb_sync_hook.mjs` が月次確定 UI から
    自動起動するので、サーバ側でやることは FiscalClose の更新と
    closing 仕訳生成のみ。
    """
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    current = fc.closed_period if fc else -1

    if period <= current:
        return "この期間は既に確定済みです。"
    if period != current + 1:
        prev_label = PERIOD_LABELS.get(current + 1, f"{current + 1}月")
        return f"先に{prev_label}を確定してください。"

    if not fc:
        fc = FiscalClose(user_id=user_id, year=year, closed_period=period)
        db.session.add(fc)
    else:
        fc.closed_period = period

    # 決算月3確定 → 損益振替仕訳を自動生成
    if period == 15:
        err = generate_closing_entries(user_id, year)
        if err:
            db.session.rollback()
            return err

    db.session.commit()
    return None


def reopen_period(user_id, year, period):
    """月次確定を解除。成功時はNone、エラー時はメッセージを返す。

    Phase E3-F-6 で旧 balance_caches テーブル無効化は撤去 (BCB に統合)。
    BCB のクリーンアップはクライアント側で行う。
    """
    fc = FiscalClose.query.filter_by(user_id=user_id, year=year).first()
    if not fc or fc.closed_period < period:
        return "この期間は確定されていません。"
    if fc.closed_period != period:
        return "最後に確定した期間のみ解除できます。"

    # 翌年度以降に確定があれば解除不可
    later = FiscalClose.query.filter(
        FiscalClose.user_id == user_id,
        FiscalClose.year > year,
        FiscalClose.closed_period >= 0,
    ).order_by(FiscalClose.year).first()
    if later:
        return (
            f"{later.year}年度に確定済み期間があるため、"
            f"{year}年の確定を解除できません。"
            f"先に{later.year}年度の確定を全て解除してください。"
        )

    # 決算月3解除 → 損益振替仕訳を削除
    if period == 15:
        delete_closing_entries(user_id, year)

    fc.closed_period = period - 1
    db.session.commit()
    return None


def generate_closing_entries(user_id, year):
    """損益振替仕訳を生成"""
    revenue_type = AccountType.query.filter_by(code="revenue").first()
    expense_type = AccountType.query.filter_by(code="expense").first()
    retained = Account.query.filter_by(user_id=user_id, system_role="retained_earnings").first()

    if not revenue_type or not expense_type or not retained:
        return "勘定科目（収益・費用・繰越利益）が見つかりません。"

    batch = f"closing-{year}-{uuid.uuid4().hex[:8]}"

    # 収益科目ごとの貸方合計（= 収益残高）
    revenue_balances = (
        db.session.query(
            Account.code,
            Account.name,
            (func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)).label("balance"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == revenue_type.id,
            JournalEntry.fiscal_year == year,
        )
        .group_by(Account.code, Account.name)
        .having(
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)
            - func.coalesce(func.sum(JournalEntryLine.debit_amount), 0) != 0
        )
        .all()
    )

    # 費用科目ごとの借方合計（= 費用残高）
    expense_balances = (
        db.session.query(
            Account.code,
            Account.name,
            (func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
             - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0)).label("balance"),
        )
        .join(JournalEntryLine, db.and_(
            JournalEntryLine.account_user_id == Account.user_id,
            JournalEntryLine.account_code == Account.code,
        ))
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .filter(
            Account.user_id == user_id,
            Account.account_type_id == expense_type.id,
            JournalEntry.fiscal_year == year,
        )
        .group_by(Account.code, Account.name)
        .having(
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0)
            - func.coalesce(func.sum(JournalEntryLine.credit_amount), 0) != 0
        )
        .all()
    )

    if not revenue_balances and not expense_balances:
        return None  # 振替不要

    lines_data = []

    # 収益振替: 借方=収益科目（ゼロに）/ 貸方=繰越利益
    total_revenue = Decimal(0)
    for acct_code, acct_name, balance in revenue_balances:
        amt = int(balance)
        if amt > 0:
            lines_data.append({
                "account_code": acct_code, "debit_amount": amt, "credit_amount": 0,
            })
            total_revenue += amt
        elif amt < 0:
            lines_data.append({
                "account_code": acct_code, "debit_amount": 0, "credit_amount": -amt,
            })
            total_revenue += amt

    # 費用振替: 貸方=費用科目（ゼロに）/ 借方=繰越利益
    total_expense = Decimal(0)
    for acct_code, acct_name, balance in expense_balances:
        amt = int(balance)
        if amt > 0:
            lines_data.append({
                "account_code": acct_code, "debit_amount": 0, "credit_amount": amt,
            })
            total_expense += amt
        elif amt < 0:
            lines_data.append({
                "account_code": acct_code, "debit_amount": -amt, "credit_amount": 0,
            })
            total_expense += amt

    # 繰越利益への振替
    net = int(total_revenue - total_expense)
    if net > 0:
        lines_data.append({
            "account_code": retained.code, "debit_amount": 0, "credit_amount": net,
        })
    elif net < 0:
        lines_data.append({
            "account_code": retained.code, "debit_amount": -net, "credit_amount": 0,
        })

    if not lines_data:
        return None

    # E3-F: サーバは MK を持たないため closing 仕訳の encrypted_blob を生成できない。
    # 暫定的に空 blob (b"") + ゼロ IV のセンチネル値を入れ、クライアント側
    # (journals_client.js) が `is_closing && encrypted_blob.length === 0` を
    # 「自動生成された損益振替仕訳」と認識して date / description / source を
    # is_closing / fiscal_year から合成・ハードコード表示する。
    # closing 仕訳生成のクライアント完全移譲は follow-up (#221) で対応する。
    # E3-F PR-D-6-4: 平文 date / description / source / fiscal_period 列は書き込まない
    # (クライアントが is_closing / fiscal_month=16 / fiscal_year から合成する)。
    entry = JournalEntry(
        user_id=user_id,
        entry_number=get_next_entry_number(user_id),
        batch_id=batch,
        is_closing=True,
        fiscal_month=16,
        fiscal_year=year,
        encrypted_blob=b"",
        blob_iv=bytes(_CLOSING_IV_LEN),
    )
    db.session.add(entry)
    db.session.flush()

    for ld in lines_data:
        db.session.add(JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            account_code=ld["account_code"],
            debit_amount=ld["debit_amount"],
            credit_amount=ld["credit_amount"],
            encrypted_blob=b"",
            blob_iv=bytes(_CLOSING_IV_LEN),
        ))

    return None


def delete_closing_entries(user_id, year):
    """自動生成した損益振替仕訳を削除"""
    entries = JournalEntry.query.filter(
        JournalEntry.user_id == user_id,
        JournalEntry.is_closing.is_(True),
        JournalEntry.fiscal_month == 16,
        JournalEntry.fiscal_year == year,
    ).all()
    for entry in entries:
        db.session.delete(entry)
