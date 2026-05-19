"""設定画面用フォーム (Phase 4 公開運用整備)。"""

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SubmitField
from wtforms.validators import DataRequired


class DeleteAccountForm(FlaskForm):
    """退会フォーム。パスワード再認証 + 同意チェックを要求する。"""

    password = PasswordField(
        "パスワード再入力",
        validators=[DataRequired()],
    )
    confirm = BooleanField(
        "全てのデータが削除されることを理解しました",
        validators=[
            DataRequired(message="削除内容への同意が必要です"),
        ],
    )
    submit = SubmitField("アカウントを削除する")
