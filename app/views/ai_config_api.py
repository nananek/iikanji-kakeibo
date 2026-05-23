"""E2EE 化された UserAIConfig API (E2 Phase E2-a、設計書 §11.5)。

クライアント側 MK で AES-256-GCM 暗号化された API キー (api_key_blob / api_key_iv)
の保管・取得を担当する。サーバは復号できない。

エンドポイント:
- GET    /api/v1/ai-config             — 設定取得 (暗号文 + メタデータ)
- PUT    /api/v1/ai-config             — 暗号文を保存 / 更新
- DELETE /api/v1/ai-config             — 削除
- POST   /api/v1/ai-config/migrate-key — Phase E2-a 限定の互換 endpoint
                                          (旧 Fernet 暗号化を一時的に復号して
                                           クライアントに返却、即座に旧カラムを
                                           NULL クリア、per-user 1 回限り)

設計書 §11.5 ですべて記述。migrate-key の per-user 1 回制約は §11.4-1078。
"""

from base64 import b64decode, b64encode
from datetime import datetime, timezone

import logging

from flask import Blueprint, current_app, g, jsonify, request

from app.extensions import db, limiter
from app.models.ai_config import UserAIConfig
from app.services.api_auth import auth_required, rate_limit_key

logger = logging.getLogger(__name__)


bp = Blueprint("ai_config_api", __name__, url_prefix="/api/v1/ai-config")


# 暗号文サイズ上限。OpenAI/Anthropic 等の API キーは ~100B 程度なので、
# AES-GCM 暗号文 (タグ込) としても 256B あれば十分。アタッカーが巨大な
# blob を送り込んでストレージ消費する攻撃を防ぐ。
MAX_API_KEY_BLOB_SIZE = 1024
# IV は AES-GCM 仕様で 12B 固定
WRAP_IV_SIZE = 12
# provider 名 (openai/anthropic/google/llama_cpp 等)
ALLOWED_PROVIDERS = {"openai", "anthropic", "google", "llama_cpp"}
MAX_MODEL_NAME_LENGTH = 100
MAX_CUSTOM_PROMPT_LENGTH = 10000


def _b64_or_error(payload: dict, key: str, *, required: bool = True):
    """payload[key] を base64 デコード。
    成功時: (bytes, None) / 失敗時: (None, "<key>...") / 未指定 + optional: (None, None)。
    例外を投げないことで CodeQL の stack-trace-exposure を回避。
    """
    val = payload.get(key)
    if val is None or val == "":
        if required:
            return None, f"{key} is required"
        return None, None
    if not isinstance(val, str):
        return None, f"{key} must be a base64 string"
    try:
        return b64decode(val, validate=True), None
    except Exception:
        return None, f"{key} is not valid base64"


def _serialize(config: UserAIConfig) -> dict:
    """UserAIConfig を JSON 返却用に整形する (api_key_encrypted は返さない)。"""
    return {
        "provider": config.provider,
        "model_name": config.model_name,
        "custom_prompt": config.custom_prompt,
        "compliance_check": config.compliance_check,
        "api_key_blob": (
            b64encode(config.api_key_blob).decode() if config.api_key_blob else None
        ),
        "api_key_iv": (
            b64encode(config.api_key_iv).decode() if config.api_key_iv else None
        ),
        "is_e2ee": config.is_e2ee,
        "migrated_at": (
            config.migrated_at.isoformat() if config.migrated_at else None
        ),
        # 旧 Fernet 形式かどうか (未移行ユーザー判定用)
        "has_legacy_key": config.api_key_encrypted is not None,
        "created_at": config.created_at.isoformat() if config.created_at else None,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


@bp.get("")
@auth_required(write=False)
@limiter.limit("60 per hour", key_func=rate_limit_key)
def get_ai_config():
    """自身の AI 設定を取得。未設定なら 404。"""
    config = UserAIConfig.query.filter_by(user_id=g.auth_user.id).first()
    if config is None:
        return jsonify(error="AI config not set"), 404
    return jsonify(_serialize(config))


@bp.put("")
@auth_required(write=True)
@limiter.limit("30 per hour", key_func=rate_limit_key)
def put_ai_config():
    """AI 設定を保存または更新 (E2EE 暗号文受け取り)。サーバは復号しない。

    Body (JSON):
        provider          : "openai" 等 (ALLOWED_PROVIDERS のいずれか)
        api_key_blob      : base64 (暗号文、AES-256-GCM タグ込)
        api_key_iv        : base64 (12B IV)
        model_name        : str (空可)
        custom_prompt     : str (空可)
        compliance_check  : bool
    """
    payload = request.get_json(silent=True) or {}

    provider = payload.get("provider")
    if provider not in ALLOWED_PROVIDERS:
        return jsonify(
            error=f"provider must be one of {sorted(ALLOWED_PROVIDERS)}"
        ), 400

    blob, err = _b64_or_error(payload, "api_key_blob")
    if err is not None:
        return jsonify(error=err), 400
    iv, err = _b64_or_error(payload, "api_key_iv")
    if err is not None:
        return jsonify(error=err), 400
    if len(iv) != WRAP_IV_SIZE:
        return jsonify(error=f"api_key_iv length must be {WRAP_IV_SIZE} bytes"), 400
    if len(blob) > MAX_API_KEY_BLOB_SIZE:
        return jsonify(
            error=f"api_key_blob too large (max {MAX_API_KEY_BLOB_SIZE} bytes)"
        ), 400

    model_name = payload.get("model_name", "") or ""
    custom_prompt = payload.get("custom_prompt", "") or ""
    compliance_check = bool(payload.get("compliance_check", False))
    if not isinstance(model_name, str) or len(model_name) > MAX_MODEL_NAME_LENGTH:
        return jsonify(
            error=f"model_name must be string up to {MAX_MODEL_NAME_LENGTH} chars"
        ), 400
    if not isinstance(custom_prompt, str) or len(custom_prompt) > MAX_CUSTOM_PROMPT_LENGTH:
        return jsonify(
            error=f"custom_prompt must be string up to {MAX_CUSTOM_PROMPT_LENGTH} chars"
        ), 400

    config = UserAIConfig.query.filter_by(user_id=g.auth_user.id).first()
    if config is None:
        config = UserAIConfig(user_id=g.auth_user.id)
        db.session.add(config)
    config.provider = provider
    config.api_key_blob = blob
    config.api_key_iv = iv
    # 旧 Fernet データは PUT で上書きされた時点で不要、念のためクリア
    config.api_key_encrypted = None
    config.model_name = model_name
    config.custom_prompt = custom_prompt
    config.compliance_check = compliance_check
    if config.migrated_at is None:
        config.migrated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(_serialize(config))


@bp.delete("")
@auth_required(write=True)
@limiter.limit("10 per hour", key_func=rate_limit_key)
def delete_ai_config():
    """AI 設定を削除。"""
    config = UserAIConfig.query.filter_by(user_id=g.auth_user.id).first()
    if config is None:
        return ("", 204)
    db.session.delete(config)
    db.session.commit()
    return ("", 204)


@bp.post("/migrate-key")
@auth_required(write=True)
@limiter.limit("3 per hour", key_func=rate_limit_key)
def migrate_key():
    """Phase E2-a 限定の互換 endpoint。

    旧 Fernet 暗号化された api_key_encrypted を一時的にサーバ復号し、平文を
    呼出元 (クライアント) に返す。**per-user 1 回限り**: 呼出成功時に
    migrated_at をセットし、以降は 410 Gone で拒否する。

    ⚠️ この endpoint は v5.0 公開後 (全ユーザー移行完了後) に必ず削除すること。
    残存させると全ユーザーの API キーをサーバ側で取得可能になる。

    🛟 リカバリ手順 (commit 後にネットワーク障害でクライアントが平文を受け取
    れずに 410 Gone で詰む稀なケース):
        1. ユーザーが既に旧 Fernet 暗号鍵を別途バックアップしているなら設定
           画面で再入力 → PUT で E2EE 形式に保存し直す
        2. バックアップがない場合は API キー紛失扱い → ユーザーが LLM
           プロバイダ管理画面で新規 API キーを発行 → 設定画面で再入力
        3. 管理者が `flask ai-config-reset-migrate-key <user_id>` で migrate-key
           を再呼出可能にするオプションはあるが、旧 Fernet データは即時 NULL
           クリアされているため再呼出しても 404 になる。実用上は (1)(2) のみ
    """
    config = UserAIConfig.query.filter_by(user_id=g.auth_user.id).first()
    if config is None:
        return jsonify(error="AI config not set"), 404

    # 既に migrate-key を呼出済みなら拒否 (per-user 1 回限り)
    if config.migrated_at is not None:
        return jsonify(
            error="already migrated; this endpoint is one-time only per user",
        ), 410

    # 旧形式データがないと migrate のしようがない
    if config.api_key_encrypted is None:
        # E2EE 形式で新規登録されたユーザー (migrate 不要)。
        # 念のため migrated_at をセットして再呼出を防ぐ
        config.migrated_at = datetime.now(timezone.utc)
        db.session.commit()
        return jsonify(error="no legacy api_key_encrypted to migrate"), 404

    # サーバ側で Fernet 復号 (本 endpoint だけが許容する例外的な平文露出)
    from app.services.ai_receipt import decrypt_api_key
    try:
        plaintext = decrypt_api_key(config.api_key_encrypted)
    except Exception:
        # Fernet 復号失敗は SECRET_KEY 変更等の運用ミス。スタックトレースを
        # サーバログに残し原因追跡を可能にする (ユーザーには汎用メッセージ)。
        current_app.logger.exception(
            "migrate_key: Fernet decryption failed for user_id=%s",
            config.user_id,
        )
        return jsonify(error="failed to decrypt legacy key (server-side issue)"), 500

    # 即座に旧カラムを NULL クリア + migrated_at セット
    config.api_key_encrypted = None
    config.migrated_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify(
        provider=config.provider,
        # plaintext は migrate-key 戻り値としてのみ存在、サーバ側はもう持たない
        api_key=plaintext.decode("utf-8") if isinstance(plaintext, bytes) else plaintext,
        model_name=config.model_name,
        custom_prompt=config.custom_prompt,
        compliance_check=config.compliance_check,
    )
