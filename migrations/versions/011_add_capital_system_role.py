"""元入金科目にsystem_role='capital'を設定"""

import sqlalchemy as sa
from alembic import op


revision = "011"
down_revision = "010"


def upgrade():
    op.execute(
        "UPDATE accounts SET system_role = 'capital' "
        "WHERE code = '3010' AND system_role IS NULL"
    )


def downgrade():
    op.execute(
        "UPDATE accounts SET system_role = NULL "
        "WHERE code = '3010' AND system_role = 'capital'"
    )
