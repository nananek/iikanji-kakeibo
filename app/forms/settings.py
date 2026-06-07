"""設定画面用フォーム (Phase 4 公開運用整備)。"""

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Optional


class DeleteAccountForm(FlaskForm):
    """退会フォーム。パスワード再認証 + 同意チェックを要求する。

    `password` フィールドは Optional とし、パスワード未設定ユーザー
    (`password_hash=NULL`) はパスワードを持たないためバリデータでは強制しない。
    view 側で `current_user.password_hash` の有無に応じて動的に再認証要否を
    判定する (GDPR データ消去権を満たすため、パスワード未設定ユーザーでも
    退会できなければならない、#385 PR-T4)。
    """

    password = PasswordField(
        "パスワード再入力 (パスワード未設定アカウントは不要)",
        validators=[Optional()],
    )
    confirm = BooleanField(
        "全てのデータが削除されることを理解しました",
        validators=[
            DataRequired(message="削除内容への同意が必要です"),
        ],
    )
    submit = SubmitField("アカウントを削除する")
