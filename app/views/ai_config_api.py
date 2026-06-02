"""E2EE 化された UserAIConfig API (設計書 §11.5)。

クライアント側 MK で AES-256-GCM 暗号化された API キー (api_key_blob / api_key_iv)
の保管・取得を担当する。サーバは復号できない。

エンドポイント:
- GET    /api/v1/ai-config — 設定取得 (暗号文 + メタデータ)
- PUT    /api/v1/ai-config — 暗号文を保存 / 更新
- DELETE /api/v1/ai-config — 削除

旧 POST /migrate-key (Phase E2-a 限定の Fernet → E2EE 移行用) は Fernet
完全廃止 (Phase E2-b) に伴い削除。
"""

from base64 import b64decode, b64encode

import logging

from flask import Blueprint, g, jsonify, request

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
# provider 名 (自家ホスト llama_cpp は E2EE 非両立につき v5.0 で廃止)
ALLOWED_PROVIDERS = {"openai", "anthropic", "google"}
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
    """UserAIConfig を JSON 返却用に整形する。"""
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
    config.model_name = model_name
    config.custom_prompt = custom_prompt
    config.compliance_check = compliance_check
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


# 旧 POST /migrate-key (Fernet → E2EE blob 移行用) は E2EE 化完了 + Fernet
# 完全廃止に伴い削除。旧 Fernet データを持ったユーザー (api_key_encrypted が
# 非 NULL のまま残っていた場合) は LLM プロバイダ管理画面で API キーを新規
# 発行し、設定画面で再登録する必要がある。
