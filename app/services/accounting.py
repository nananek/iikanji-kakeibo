"""仕訳自動生成サービス"""

from app.extensions import db
from app.models.journal import JournalEntry, JournalEntryLine


def get_next_entry_number(user_id):
    """次の伝票番号を取得"""
    max_num = (
        db.session.query(db.func.max(JournalEntry.entry_number))
        .filter(JournalEntry.user_id == user_id)
        .scalar()
    )
    return (max_num or 0) + 1


def create_journal_entry(user_id, lines_data, *, fiscal_year, fiscal_month,
                         batch_id=None, encrypted_blob=None, blob_iv=None,
                         is_closing=False, commit=True):
    """仕訳伝票を直接作成する

    Args:
        lines_data: list of dict with key: encrypted_blob/blob_iv
            (Phase E3: クライアント側で AES-GCM 暗号化済の line 本体)。
            #338 item5: 平文 account_code / debit_amount / credit_amount は
            DB に書かない (line 本体は encrypted_blob のみ。集計・科目存在・貸借は
            クライアント + 監査時検査の責務へ §12.11/§13)。後方互換で lines_data に
            これらが残っていても無視する。
        fiscal_year: 平文の年度フィルタ用メタ列 (date 暗号化後の代替)。
        fiscal_month: 平文の計上期間メタ列 (0=期首, 1-12=月, 13-15=決算整理,
            16=損益振替)。
        encrypted_blob/blob_iv: Phase E3 - クライアント側で AES-GCM 暗号化された
            entry 本体 (date / description / source / batch_id / fiscal_period の
            暗号化版)。両方セット or 両方 None。
        is_closing: 損益振替 (決算振替) 仕訳なら True (#338 item1)。クライアントが
            暗号化生成した closing 仕訳を fiscal_month=16 / is_closing=True で保存
            する専用エンドポイント (close_closing) から渡される。

    E3-F PR-D-6-6: wire 平文除去。date / description / source / fiscal_period は
        request からも引数からも撤去した。entry の平文メタは fiscal_year /
        fiscal_month のみ (両者ともクライアントが算出して必須送信する)。entry
        本体の実値 (日付・摘要・source 等) は encrypted_blob に格納済。
        commit: False を指定するとセッションを commit せず flush のみ行う。
            複数 entry をまとめて 1 トランザクションにする batch API 用。
    """
    # #338 item5: サーバは平文金額を持たなくなったため貸借一致をサーバ側で検査
    # できない (§12.11/§13 でクライアント + 監査時検査の責務へ移行)。
    if (encrypted_blob is None) != (blob_iv is None):
        raise ValueError("encrypted_blob と blob_iv は同時に指定が必要です。")
    # 多層防御: API 以外の caller が短い IV で保存しないよう service 層でも検査。
    if blob_iv is not None and len(blob_iv) != 12:
        raise ValueError(
            "blob_iv は 12B (AES-GCM IV) である必要があります。",
        )

    entry = JournalEntry(
        user_id=user_id,
        entry_number=get_next_entry_number(user_id),
        batch_id=batch_id,
        encrypted_blob=encrypted_blob,
        blob_iv=blob_iv,
        # E3-F PR-D-6-6: 平文 date / description / source / fiscal_period 列は
        # DROP 済 (055)。entry の平文メタは fiscal_year / fiscal_month のみ
        # (クライアント算出値をそのまま populate する)。
        fiscal_year=fiscal_year,
        fiscal_month=fiscal_month,
        is_closing=is_closing,
    )
    db.session.add(entry)
    db.session.flush()

    for line_data in lines_data:
        line_blob = line_data.get("encrypted_blob")
        line_iv = line_data.get("blob_iv")
        if (line_blob is None) != (line_iv is None):
            raise ValueError(
                "line の encrypted_blob と blob_iv は同時に指定が必要です。",
            )
        if line_iv is not None and len(line_iv) != 12:
            raise ValueError(
                "line の blob_iv は 12B (AES-GCM IV) である必要があります。",
            )
        # #338 item8 (068): 平文 account_code / debit / credit 列は物理 DROP 済。
        # line 本体は encrypted_blob のみ。
        line = JournalEntryLine(
            journal_entry_id=entry.id,
            account_user_id=user_id,
            encrypted_blob=line_blob,
            blob_iv=line_iv,
        )
        db.session.add(line)

    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return entry
