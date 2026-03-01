from flask_wtf import FlaskForm
from wtforms import DateField, StringField, SubmitField, FieldList, FormField, IntegerField, SelectField
from wtforms.validators import DataRequired


class JournalLineForm(FlaskForm):
    """仕訳明細行（サブフォーム）"""
    class Meta:
        csrf = False

    account_code = SelectField("勘定科目", coerce=str, validators=[DataRequired()])
    debit_amount = IntegerField("借方", default=0)
    credit_amount = IntegerField("貸方", default=0)
    description = StringField("摘要")


class JournalForm(FlaskForm):
    date = DateField("日付", validators=[DataRequired()])
    fiscal_period = SelectField("計上期間", coerce=str, choices=[
        ("", "期中"),
        ("0", "期首振戻月"),
        ("13", "決算月1"),
        ("14", "決算月2"),
        ("15", "決算月3"),
    ], default="")
    description = StringField("摘要", validators=[DataRequired()])
    submit = SubmitField("登録")
