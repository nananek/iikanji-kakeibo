"""OFXファイルパースサービス"""

from io import BytesIO

from ofxparse import OfxParser


def parse_ofx(file_bytes):
    """OFXファイルをパースし、CSV/Webと同じ行形式で返す

    Returns:
        dict: {
            "account_id": str,  # OFX内の口座番号
            "account_type": str,  # 口座種別
            "rows": list[dict],  # 取引行リスト
        }
    """
    ofx = OfxParser.parse(BytesIO(file_bytes))
    account = ofx.account

    rows = []
    transactions = account.statement.transactions if account.statement else []

    for i, txn in enumerate(transactions, start=1):
        # description: payee と memo を結合（重複除去）
        parts = []
        payee = (txn.payee or "").strip()
        memo = (txn.memo or "").strip()
        if payee:
            parts.append(payee)
        if memo and memo != payee:
            parts.append(memo)
        description = " ".join(parts)

        # amount: 正=入金、負=出金
        amount = int(txn.amount)
        deposit = amount if amount > 0 else 0
        withdrawal = abs(amount) if amount < 0 else 0

        # date: datetime → date文字列
        txn_date = txn.date.strftime("%Y-%m-%d") if txn.date else None

        rows.append({
            "row_num": i,
            "date": txn_date,
            "description": description,
            "deposit": deposit,
            "withdrawal": withdrawal,
        })

    return {
        "account_id": getattr(account, "account_id", "") or "",
        "account_type": getattr(account, "account_type", "") or "",
        "rows": rows,
    }
