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
    transaction_type = SelectField(
        "種類",
        choices=[("expense", "支出"), ("income", "収入")],
        validators=[DataRequired()],
    )
    payment_account_id = SelectField(
        "支払元 / 入金先", coerce=int, validators=[DataRequired()]
    )
    category_account_id = SelectField(
        "費目", coerce=int, validators=[DataRequired()]
    )
    amount = IntegerField(
        "金額", validators=[DataRequired(), NumberRange(min=1, message="1円以上を入力してください")]
    )
    description = StringField("摘要", validators=[DataRequired()])
    submit = SubmitField("登録")
