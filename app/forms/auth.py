from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField
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
    user_type = SelectField(
        "アカウント種別",
        choices=[("personal", "個人"), ("auditor", "監査用（税理士・公認会計士）")],
        default="personal",
    )
    submit = SubmitField("登録")

    def validate_username(self, field):
        if User.query.filter_by(username=field.data).first():
            raise ValidationError("このユーザー名は既に使われています。")

    def validate_email(self, field):
        if User.query.filter_by(email=field.data).first():
            raise ValidationError("このメールアドレスは既に登録されています。")
