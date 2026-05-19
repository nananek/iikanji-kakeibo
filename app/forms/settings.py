"""設定画面用フォーム (Phase 4 公開運用整備)。"""

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Optional


class DeleteAccountForm(FlaskForm):
    """退会フォーム。パスワード再認証 + 同意チェックを要求する。

    `password` フィールドは Optional とし、Passkey 専用ユーザー
    (`passkey_only_login=True`) はパスワードを持たないためバリデータでは
    強制しない。view 側で `current_user.passkey_only_login` に応じて
    動的に再認証要否を判定する (GDPR データ消去権を満たすため、
    Passkey 専用ユーザーでも退会できなければならない)。
    """

    password = PasswordField(
        "パスワード再入力 (Passkey 専用アカウントは不要)",
        validators=[Optional()],
    )
    confirm = BooleanField(
        "全てのデータが削除されることを理解しました",
        validators=[
            DataRequired(message="削除内容への同意が必要です"),
        ],
    )
    submit = SubmitField("アカウントを削除する")
