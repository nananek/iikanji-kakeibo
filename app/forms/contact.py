"""お問い合わせフォーム (Phase 4 公開運用整備)。"""

from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional, Regexp


# ヘッダ行 (Subject / 返信先表示) に流れる値には改行を一切許可しない。
# `render_email` 側でも `\r\n` を空白に置換する多重防御を実施しているが、
# 入口バリデーションで早期拒否してユーザーに分かりやすいエラーを返す。
_NO_NEWLINE = Regexp(r"^[^\r\n]*$", message="改行は使用できません")


class ContactForm(FlaskForm):
    name = StringField(
        "お名前",
        validators=[DataRequired(), Length(max=100), _NO_NEWLINE],
    )
    email = StringField(
        "メールアドレス",
        validators=[DataRequired(), Email(), Length(max=200), _NO_NEWLINE],
    )
    subject_line = StringField(
        "件名 (任意)",
        validators=[Optional(), Length(max=200), _NO_NEWLINE],
    )
    message = TextAreaField(
        "お問い合わせ内容",
        validators=[DataRequired(), Length(min=10, max=5000)],
    )
    submit = SubmitField("送信する")
