"""E6 #113 §15.3: flask v5-migrate-cleanup CLI のテスト。

廃止 provider 'llama_cpp' の user_ai_configs 行を削除し、webhook_configs /
ai_drafts.discord_* の残存件数を監査表示する。webhook/discord の物理削除は
マイグレーション 064/065 が担うため、本 CLI は表示のみで削除しない。
"""

from uuid import uuid4

from app.models import User
from app.models.ai_config import UserAIConfig
from app.models.ai_draft import AIDraft


def _user(db, name=None):
    name = name or f"u{uuid4().hex[:8]}"
    u = User(username=name, email=f"{name}@e.com")
    u.set_password("pw")
    db.session.add(u)
    db.session.commit()
    return u


def _ai_config(db, user, provider):
    cfg = UserAIConfig(user_id=user.id, provider=provider)
    db.session.add(cfg)
    db.session.commit()
    return cfg


def _setup(db):
    """llama_cpp 1 件 + openai 1 件 + draft 1 件。

    webhook_configs テーブル (migration 064) と ai_drafts.discord_* カラム
    (migration 065) はいずれも E6 で DROP 済みのため、CLI の監査表示では
    どちらも「該当なし」になる。
    """
    u_llama, u_openai = _user(db), _user(db)
    llama_cfg = _ai_config(db, u_llama, "llama_cpp")
    openai_cfg = _ai_config(db, u_openai, "openai")

    draft = AIDraft(
        user_id=u_openai.id, image_key="k", image_mime="image/png",
    )
    db.session.add(draft)
    db.session.commit()
    return llama_cfg, openai_cfg


def test_dry_run_reports_and_deletes_nothing(db, app):
    llama_cfg, openai_cfg = _setup(db)
    llama_id, openai_id = llama_cfg.id, openai_cfg.id

    result = app.test_cli_runner().invoke(args=["v5-migrate-cleanup", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run]" in result.output
    # llama_cpp 1 件が削除対象として表示される
    assert "provider='llama_cpp': 1 件" in result.output
    # webhook_configs テーブル (064) / ai_drafts.discord_* カラム (065) は
    # いずれも DROP 済 → 該当なし
    assert "webhook_configs: 該当なし" in result.output
    assert "ai_drafts.discord_*: 該当なし" in result.output

    db.session.expire_all()
    # 何も削除されていない
    assert db.session.get(UserAIConfig, llama_id) is not None
    assert db.session.get(UserAIConfig, openai_id) is not None


def test_no_flag_defaults_to_dry_run(db, app):
    llama_cfg, _ = _setup(db)
    llama_id = llama_cfg.id

    result = app.test_cli_runner().invoke(args=["v5-migrate-cleanup"])
    assert result.exit_code == 0
    assert "[dry-run]" in result.output

    db.session.expire_all()
    assert db.session.get(UserAIConfig, llama_id) is not None


def test_execute_deletes_only_llama_cpp(db, app):
    llama_cfg, openai_cfg = _setup(db)
    llama_id, openai_id = llama_cfg.id, openai_cfg.id

    result = app.test_cli_runner().invoke(args=["v5-migrate-cleanup", "--execute"])
    assert result.exit_code == 0
    assert "deleted user_ai_configs (llama_cpp)=1" in result.output

    db.session.expire_all()
    # llama_cpp は削除、openai は残る
    assert db.session.get(UserAIConfig, llama_id) is None
    assert db.session.get(UserAIConfig, openai_id) is not None
    # ai_drafts は CLI の対象外 (削除しない)
    assert AIDraft.query.count() == 1


def test_dry_run_takes_precedence_over_execute(db, app):
    """--dry-run と --execute 併用時は安全側 (dry-run) を優先する。"""
    llama_cfg, _ = _setup(db)
    llama_id = llama_cfg.id

    result = app.test_cli_runner().invoke(
        args=["v5-migrate-cleanup", "--dry-run", "--execute"]
    )
    assert result.exit_code == 0
    assert "[dry-run]" in result.output

    db.session.expire_all()
    assert db.session.get(UserAIConfig, llama_id) is not None


def test_execute_with_no_llama_cpp(db, app):
    u = _user(db)
    _ai_config(db, u, "anthropic")

    result = app.test_cli_runner().invoke(args=["v5-migrate-cleanup", "--execute"])
    assert result.exit_code == 0
    assert "deleted user_ai_configs (llama_cpp)=0" in result.output
    assert UserAIConfig.query.count() == 1
