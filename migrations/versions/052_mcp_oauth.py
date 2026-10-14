"""MCP クライアント向け OAuth 2.1 (Authorization Code + PKCE / 動的登録)

Revision ID: 052_mcp_oauth
Revises: 051_drop_auto_import_tables
Create Date: 2026-10-15
"""

from alembic import op
import sqlalchemy as sa


revision = "052_mcp_oauth"
down_revision = "051_drop_auto_import_tables"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column("client_secret_hash", sa.String(64), nullable=True),
        sa.Column("client_name", sa.String(200), nullable=True),
        sa.Column("redirect_uris_json", sa.Text(), nullable=False),
        sa.Column(
            "grant_types", sa.String(200), nullable=False,
            server_default="authorization_code refresh_token",
        ),
        sa.Column(
            "response_types", sa.String(100), nullable=False, server_default="code"
        ),
        sa.Column(
            "token_endpoint_auth_method", sa.String(50), nullable=False,
            server_default="none",
        ),
        sa.Column("scope", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_oauth_clients_client_id", "oauth_clients", ["client_id"], unique=True
    )

    op.create_table(
        "oauth_authorization_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("client_id", sa.String(64), nullable=False),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("redirect_uri", sa.String(1000), nullable=False),
        sa.Column("scopes", sa.String(500), nullable=True),
        sa.Column("code_challenge", sa.String(200), nullable=False),
        sa.Column(
            "code_challenge_method", sa.String(10), nullable=False,
            server_default="S256",
        ),
        sa.Column("resource", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_oauth_authorization_codes_code_hash",
        "oauth_authorization_codes", ["code_hash"], unique=True,
    )
    op.create_index(
        "ix_oauth_authorization_codes_client_id",
        "oauth_authorization_codes", ["client_id"],
    )

    # oauth_tokens への追加カラム (すべて nullable で既存トークンに影響なし)
    op.add_column(
        "oauth_tokens", sa.Column("scopes", sa.String(500), nullable=True)
    )
    op.add_column(
        "oauth_devices", sa.Column("scopes", sa.String(500), nullable=True)
    )
    op.add_column(
        "oauth_tokens", sa.Column("expires_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "oauth_tokens", sa.Column("client_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "oauth_tokens",
        sa.Column("refresh_token_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "oauth_tokens",
        sa.Column("refresh_token_prefix", sa.String(20), nullable=True),
    )
    op.add_column(
        "oauth_tokens",
        sa.Column("refresh_expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_oauth_tokens_refresh_token_hash",
        "oauth_tokens", ["refresh_token_hash"], unique=True,
    )


def downgrade():
    op.drop_index("ix_oauth_tokens_refresh_token_hash", table_name="oauth_tokens")
    op.drop_column("oauth_tokens", "refresh_expires_at")
    op.drop_column("oauth_tokens", "refresh_token_prefix")
    op.drop_column("oauth_tokens", "refresh_token_hash")
    op.drop_column("oauth_tokens", "client_id")
    op.drop_column("oauth_tokens", "expires_at")
    op.drop_column("oauth_tokens", "scopes")
    op.drop_column("oauth_devices", "scopes")

    op.drop_index(
        "ix_oauth_authorization_codes_client_id",
        table_name="oauth_authorization_codes",
    )
    op.drop_index(
        "ix_oauth_authorization_codes_code_hash",
        table_name="oauth_authorization_codes",
    )
    op.drop_table("oauth_authorization_codes")

    op.drop_index("ix_oauth_clients_client_id", table_name="oauth_clients")
    op.drop_table("oauth_clients")
