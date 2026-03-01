from flask_wtf import FlaskForm
from wtforms import (
    DateField,
    SelectField,
    IntegerField,
    StringField,
    SubmitField,
)
from wtforms.validators import DataRequired, NumberRange


class CashbookForm(FlaskForm):
    date = DateField("日付", validators=[DataRequired()])
    fiscal_period = SelectField("計上期間", coerce=str, choices=[
        ("", "期中"),
        ("0", "期首振戻月"),
        ("13", "決算月1"),
        ("14", "決算月2"),
        ("15", "決算月3"),
    ], default="")
    transaction_type = SelectField(
        "種類",
        choices=[("expense", "支出"), ("income", "収入"), ("transfer", "資金移動")],
        validators=[DataRequired()],
    )
    payment_account_code = StringField(
        "支払元 / 入金先", validators=[DataRequired()]
    )
    category_account_code = StringField(
        "費目", validators=[DataRequired()]
    )
    amount = IntegerField(
        "金額", validators=[DataRequired(message="金額を入力してください。")]
    )
    description = StringField("摘要", validators=[DataRequired()])
    submit = SubmitField("登録")
