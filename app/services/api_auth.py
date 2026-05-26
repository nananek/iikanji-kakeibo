"""Bearer (APIKey + OAuth Token) + Flask-Login セッションの統合認証。

E2EE wrapped_keys API (E1 #108) で導入。将来 (PR-D 以降) の追加 API でも
同じ認証ロジックを使い回すため共通モジュール化。

将来的に `app/views/api.py` の `api_key_required` もこのモジュールを使う
ように refactor する (TODO: 別 PR で実施)。
"""

from __future__ import annotations

import functools
from datetime import datetime, timezone

from flask import g, jsonify, request, session
from flask_login import current_user

from app.extensions import db
from app.models.api_key import APIKey
from app.models.audit import AuditGrant
from app.models.oauth import OAuthToken
from app.models.user import User


def resolve_bearer_or_session(
    write: bool = False, allow_session: bool = True,
    scope: str | None = None,
) -> tuple[User | None, tuple | None]:
    """Authorization ヘッダ or Flask-Login セッションから User を解決する。

    - `Authorization: Bearer <ikt_...>` → OAuthToken (read_only なら write 拒否)
    - `Authorization: Bearer <ik_...>` → APIKey (scope 指定時はそのスコープを要求)
    - ヘッダなし & `allow_session=True` → Flask-Login の current_user (scope 不問)

    scope: API キー認証時のみ scope check を行う。OAuth トークンは全 scope を
    暗黙的に持つ、セッション認証は scope の概念がない (UI 経由なのでユーザー
    本人の意思とみなす)。

    戻り値: `(user, error_response)` のタプル。エラー時は `(None, (response, status))`、
    成功時は `(user, None)`。エラーレスポンスは Flask response tuple。
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw = auth[7:]
        now = datetime.now(timezone.utc)
        # OAuth Device Flow Token
        if raw.startswith("ikt_"):
            token_hash = OAuthToken.hash_token(raw)
            token = OAuthToken.query.filter_by(
                token_hash=token_hash, is_active=True
            ).first()
            if not token:
                return None, (jsonify(error="Invalid token"), 401)
            if write and token.read_only:
                return None, (jsonify(error="read-only token"), 403)
            token.last_used_at = now
            db.session.commit()
            user = db.session.get(User, token.user_id)
            if user is None:
                return None, (jsonify(error="User not found"), 401)
            return user, None
        # 従来の APIKey
        key_hash = APIKey.hash_key(raw)
        api_key = APIKey.query.filter_by(
            key_hash=key_hash, is_active=True
        ).first()
        if not api_key:
            return None, (jsonify(error="Invalid API key"), 401)
        if scope and not api_key.has_scope(scope):
            return None, (jsonify(
                error=f"この API キーには {scope} 権限がありません。"
            ), 403)
        api_key.last_used_at = now
        db.session.commit()
        user = db.session.get(User, api_key.user_id)
        if user is None:
            return None, (jsonify(error="User not found"), 401)
        return user, None

    # Web セッション
    if allow_session and current_user.is_authenticated:
        # 監査代理閲覧中なら effective user (= 対象オーナー) を返す。
        # write 操作は permission_level=3 (full access) のみ許可。
        acting_as = session.get("acting_as_user_id")
        if acting_as:
            grant = AuditGrant.query.filter_by(
                owner_user_id=acting_as,
                auditor_user_id=current_user.id,
            ).first()
            # Lv2 grant が submitted 状態でなくなった (オーナーが提出取消した)
            # 場合もアクセス遮断する。auditor.switch() ビューと同じ条件。
            if not grant or (
                grant.permission_level == 2 and grant.status != "submitted"
            ):
                session.pop("acting_as_user_id", None)
                session.pop("acting_as_permission_level", None)
                return current_user._get_current_object(), None
            # 権限レベルは DB grant から直接読む (session キャッシュを信頼しない:
            # オーナーの権限変更がリアルタイム反映される、SECRET_KEY 漏洩時の
            # セッション偽造でも grant.permission_level は守られる)。
            #
            # Lv1 (集計結果のみ閲覧) は API 経由の代理閲覧を一律遮断する。
            # API は生の仕訳・証憑データを返すため、Lv1 の仕様 (集計のみ) と
            # 整合しない。Lv1 監査者は Web UI のサーバ render 集計ページ
            # のみ利用可能。
            if grant.permission_level < 2:
                return None, (jsonify(
                    error="Lv1 監査アカウントは API 経由の代理閲覧をサポートしません。"
                ), 403)
            if write and grant.permission_level < 3:
                return None, (jsonify(
                    error="代理閲覧中の書込操作は権限レベル 3 (full access) のみ可能です。"
                ), 403)
            effective_user = db.session.get(User, acting_as)
            if effective_user is None:
                return None, (jsonify(error="User not found"), 401)
            return effective_user, None
        return current_user._get_current_object(), None
    return None, (jsonify(error="Authentication required"), 401)


def auth_required(
    write: bool = False, allow_session: bool = True,
    scope: str | None = None,
):
    """Bearer + (オプションで) Web セッションの統合デコレータ。

    認証成功時は `g.auth_user` に User をセットし、エンドポイント関数を呼ぶ。
    エンドポイントは `g.auth_user.id` で自身のリソースをフィルタする。

    write=True なら OAuth read-only トークンを 403 で拒否。
    allow_session=False ならヘッダ必須 (Bearer 専用 endpoint 用)。
    scope を指定すると API キー認証時に当該 scope を要求する (OAuth トークン
    とセッション認証は scope 不問)。
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            user, err = resolve_bearer_or_session(
                write=write, allow_session=allow_session, scope=scope,
            )
            if err is not None:
                return err
            g.auth_user = user
            return f(*args, **kwargs)
        return wrapper
    return decorator


def rate_limit_key() -> str:
    """レート制限の per-user キー。

    Flask-Limiter は before_request で評価するため、`@auth_required` ラッパー
    が `g.auth_user` を設定する前に呼ばれる。フォールバック順:

    1. `g.auth_user.id` (`@auth_required` 内で明示セットされた場合のみ — 通常は届かない)
    2. Flask-Login の `current_user` (セッション認証は user_loader で既に確定)
    3. IP アドレス (Bearer 認証時のフォールバック、将来 PR で per-token キーに拡張可能)
    """
    user = getattr(g, "auth_user", None)
    if user is not None:
        return f"user:{user.id}"
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return request.remote_addr or "anonymous"
