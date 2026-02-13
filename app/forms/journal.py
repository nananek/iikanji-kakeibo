from flask_wtf import FlaskForm
from wtforms import DateField, StringField, SubmitField, FieldList, FormField, IntegerField, SelectField
from wtforms.validators import DataRequired


class JournalLineForm(FlaskForm):
    """仕訳明細行（サブフォーム）"""
    class Meta:
        csrf = False

    account_id = SelectField("勘定科目", coerce=int, validators=[DataRequired()])
    debit_amount = IntegerField("借方", default=0)
    credit_amount = IntegerField("貸方", default=0)
    description = StringField("摘要")


class JournalForm(FlaskForm):
    date = DateField("日付", validators=[DataRequired()])
    description = StringField("摘要", validators=[DataRequired()])
    submit = SubmitField("登録")
