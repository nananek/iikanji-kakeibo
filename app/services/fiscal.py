"""月次確定・決算期間管理サービス"""

from datetime import date

from sqlalchemy import and_, or_

from app.extensions import db
from app.models.account import Account
from app.models.fiscal import FiscalClose
from app.models.journal import JournalEntry
from app.models.user import User


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
    """月次確定を実行 (period 0-14)。成功時はNone、エラー時はメッセージを返す。

    Phase E3-F-6 で旧 balance_caches テーブル更新は撤去 (BCB に統合)。
    BCB の sync はクライアント側 `bcb_sync_hook.mjs` が月次確定 UI から
    自動起動するので、サーバ側でやることは FiscalClose の更新のみ。

    #338 item1: 決算月3 (period15) の確定 + 損益振替 (closing) 生成は専用
    エンドポイント POST /api/v1/fiscal/close-closing が担う (サーバは MK を
    持たず closing を暗号化生成できないため、クライアントが集計・暗号化して送る)。
    この関数は period15 を受け付けない (caller の fiscal_close view が 422 で弾くが、
    view を経由しない caller が FiscalClose を closing 仕訳なしで 15 へ進めるのを防ぐ
    多層防御として本関数でも明示的に拒否する)。
    """
    if period == 15:
        return "決算月3は決算画面の確定ボタン (close-closing) から確定してください。"

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


# #338 item1: 旧 generate_closing_entries (サーバ側で平文 debit_amount/credit_amount を
# SQL SUM して損益振替を生成し encrypted_blob=b"" センチネルで保存していた) は撤去した。
# closing 仕訳の集計・暗号化生成はクライアントへ移譲し (closing.js)、専用エンドポイント
# POST /api/v1/fiscal/close-closing が受け取って保存する。これにより closing 仕訳も
# 実 encrypted_blob を持ち、平文 account_code/debit/credit 列の DROP (item8) が解放される。


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
