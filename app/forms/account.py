from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, BooleanField, SubmitField
from wtforms.validators import DataRequired, Length, Optional


class AccountForm(FlaskForm):
    code = StringField("科目コード", validators=[DataRequired(), Length(max=10)])
    name = StringField("科目名", validators=[DataRequired(), Length(max=100)])
    account_type_id = SelectField("科目区分", coerce=int, validators=[DataRequired()])
    description = StringField("説明", validators=[Optional(), Length(max=255)])
    tax_category = SelectField(
        "確定申告区分",
        choices=[
            ("", "なし"),
            ("social_insurance", "社会保険料控除"),
            ("life_insurance", "生命保険料控除"),
            ("earthquake_insurance", "地震保険料控除"),
            ("medical", "医療費控除"),
            ("donation", "寄附金控除"),
            ("ideco", "小規模企業共済等掛金控除"),
            ("withholding_tax", "源泉所得税"),
            ("resident_tax", "住民税"),
        ],
        validators=[Optional()],
    )
    is_active = BooleanField("有効", default=True)
    submit = SubmitField("保存")
