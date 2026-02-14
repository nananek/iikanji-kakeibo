from flask_wtf import FlaskForm
from wtforms import DateField, StringField, IntegerField, SubmitField
from wtforms.validators import DataRequired, NumberRange, Optional


class MedicalExpenseForm(FlaskForm):
    date = DateField("支払日", validators=[DataRequired()])
    patient_name = StringField("医療を受けた人", validators=[DataRequired()])
    hospital_name = StringField("病院・薬局名", validators=[DataRequired()])
    treatment_description = StringField("治療内容", validators=[DataRequired()])
    amount_paid = IntegerField(
        "支払った医療費",
        validators=[DataRequired(), NumberRange(min=1)],
    )
    insurance_reimbursement = IntegerField(
        "保険等で補填される金額", default=0, validators=[Optional(), NumberRange(min=0)]
    )
    payment_account_id = IntegerField(
        "支払元", validators=[DataRequired()]
    )
    submit = SubmitField("登録")
