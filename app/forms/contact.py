"""お問い合わせフォーム (Phase 4 公開運用整備)。"""

from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Email, Length, Optional


class ContactForm(FlaskForm):
    name = StringField(
        "お名前",
        validators=[DataRequired(), Length(max=100)],
    )
    email = StringField(
        "メールアドレス",
        validators=[DataRequired(), Email(), Length(max=200)],
    )
    subject_line = StringField(
        "件名 (任意)",
        validators=[Optional(), Length(max=200)],
    )
    message = TextAreaField(
        "お問い合わせ内容",
        validators=[DataRequired(), Length(min=10, max=5000)],
    )
    submit = SubmitField("送信する")
