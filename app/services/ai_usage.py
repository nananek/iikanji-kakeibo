"""外部 AI API 利用履歴の集計・絞込ヘルパー

`AIUsageLog` テーブルを期間・プロバイダー・機能で絞り込んで集計する。
PostgreSQL / SQLite 双方で動作するよう、月別グループ化は方言を切り替える。
"""

from datetime import date, datetime, timezone

from sqlalchemy import func

from app.extensions import db
from app.models.ai_usage_log import AIUsageLog


def _apply_filters(query, user_id, *, start=None, end=None,
                   provider=None, feature=None):
    """共通フィルタ: user_id 必須 + 任意の期間/プロバイダー/機能。"""
    query = query.filter(AIUsageLog.user_id == user_id)
    if start is not None:
        # start を 00:00 UTC として解釈
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        query = query.filter(AIUsageLog.created_at >= start_dt)
    if end is not None:
        # end は終端日の翌日 00:00 までを含める
        from datetime import timedelta
        end_dt = datetime.combine(end + timedelta(days=1),
                                  datetime.min.time(), tzinfo=timezone.utc)
        query = query.filter(AIUsageLog.created_at < end_dt)
    if provider:
        query = query.filter(AIUsageLog.provider == provider)
    if feature:
        query = query.filter(AIUsageLog.feature == feature)
    return query


def _month_expr():
    """`created_at` を YYYY-MM 文字列にする式 (PostgreSQL/SQLite 両対応)。"""
    dialect = db.engine.dialect.name
    if dialect == "sqlite":
        return func.strftime("%Y-%m", AIUsageLog.created_at)
    return func.to_char(AIUsageLog.created_at, "YYYY-MM")


def query_logs(user_id, *, start=None, end=None, provider=None, feature=None,
               page=1, per_page=50):
    """フィルタ付きでログを取得 (新しい順)。

    Returns:
        (items, total, pages, page)
    """
    q = _apply_filters(
        AIUsageLog.query.order_by(AIUsageLog.created_at.desc()),
        user_id, start=start, end=end, provider=provider, feature=feature,
    )
    total = q.count()
    items = q.offset((page - 1) * per_page).limit(per_page).all()
    pages = (total + per_page - 1) // per_page if per_page else 1
    return items, total, pages, page


def iter_logs_for_export(user_id, *, start=None, end=None,
                        provider=None, feature=None):
    """CSV エクスポート用にイテレータでログを返す (新しい順)。"""
    q = _apply_filters(
        AIUsageLog.query.order_by(AIUsageLog.created_at.desc()),
        user_id, start=start, end=end, provider=provider, feature=feature,
    )
    return q.all()


def monthly_summary(user_id, *, start=None, end=None,
                    provider=None, feature=None):
    """月 × プロバイダーごとに件数 + トークン合計を集計する。

    Returns:
        list of dict: [{month: "2026-05", provider: "openai",
                        count: 12, input_tokens: 1234, output_tokens: 567,
                        total_tokens: 1801}, ...]
        月降順 (新しい順) + プロバイダー昇順
    """
    month = _month_expr().label("month")
    q = (
        db.session.query(
            month,
            AIUsageLog.provider,
            func.count().label("count"),
            func.coalesce(func.sum(AIUsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(AIUsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(AIUsageLog.total_tokens), 0).label("total_tokens"),
        )
        .filter(AIUsageLog.user_id == user_id)
    )
    if start is not None:
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        q = q.filter(AIUsageLog.created_at >= start_dt)
    if end is not None:
        from datetime import timedelta
        end_dt = datetime.combine(end + timedelta(days=1),
                                  datetime.min.time(), tzinfo=timezone.utc)
        q = q.filter(AIUsageLog.created_at < end_dt)
    if provider:
        q = q.filter(AIUsageLog.provider == provider)
    if feature:
        q = q.filter(AIUsageLog.feature == feature)

    q = q.group_by(month, AIUsageLog.provider)
    q = q.order_by(month.desc(), AIUsageLog.provider.asc())
    rows = q.all()
    return [
        {
            "month": r.month,
            "provider": r.provider,
            "count": int(r.count or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
        }
        for r in rows
    ]


def current_month_summary(user_id):
    """当月 (UTC) の provider 別集計。設定画面の「今月の使用量」カード用。"""
    today = datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    return monthly_summary(user_id, start=start, end=today)


def delete_all_for_user(user_id):
    """指定ユーザーの全ログを削除する。本人による履歴クリア用。"""
    deleted = (
        AIUsageLog.query.filter_by(user_id=user_id).delete(synchronize_session=False)
    )
    db.session.commit()
    return deleted


# UI で使うフィルタ選択肢
FEATURE_LABELS = {
    "receipt_analyze": "領収書解析 (旧)",
    "receipt_round1": "AI証憑 R1 (書類解析)",
    "receipt_round2": "AI証憑 R2 (仕訳案生成)",
    "voucher_attach": "既存仕訳への証憑照合",
    "web_extract": "Web明細抽出",
    "category_suggest": "勘定科目推定",
    "csv_columns_detect": "CSV列マッピング検出",
    "csv_reconcile_ai": "CSV-仕訳 AI 照合",
}


STATUS_LABELS = {
    "ok": "成功",
    "http_error": "HTTPエラー",
    "timeout": "タイムアウト",
    "parse_error": "解析エラー",
    "other_error": "その他エラー",
}
