from flask_wtf import FlaskForm
from wtforms import BooleanField, StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError

from app.models.user import User


class LoginForm(FlaskForm):
    username = StringField("ユーザー名", validators=[DataRequired()])
    password = PasswordField("パスワード", validators=[DataRequired()])
    submit = SubmitField("ログイン")


class RegisterForm(FlaskForm):
    username = StringField(
        "ユーザー名", validators=[DataRequired(), Length(min=3, max=80)]
    )
    email = StringField("メールアドレス", validators=[DataRequired(), Email()])
    password = PasswordField(
        "パスワード", validators=[DataRequired(), Length(min=8)]
    )
    password_confirm = PasswordField(
        "パスワード（確認）",
        validators=[DataRequired(), EqualTo("password", message="パスワードが一致しません")],
    )
    accept_terms = BooleanField(
        "利用規約・プライバシーポリシーに同意します",
        validators=[
            DataRequired(message="利用規約・プライバシーポリシーへの同意が必要です"),
        ],
    )
    submit = SubmitField("登録")

    def validate_username(self, field):
        if User.query.filter_by(username=field.data).first():
            raise ValidationError("このユーザー名は既に使われています。")

    def validate_email(self, field):
        if User.query.filter_by(email=field.data).first():
            raise ValidationError("このメールアドレスは既に登録されています。")
