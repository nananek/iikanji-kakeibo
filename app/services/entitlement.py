"""エンタイトルメント (有償機能ゲート) の本体側ロジック。

本体は支払い状況の永続データを持たない。`BillingClient` を経由して
billing サービス (Phase 3 で別コンテナとして実装) に照会する設計。

公開ロードマップ Epic #64 / Phase 2 #67 のアーキテクチャ方針:

- **公開 SaaS モード** (`BILLING_BACKEND=http`): billing コンテナに
  HTTP 照会。実装は Phase 3 で `HttpBillingClient` として追加予定。
- **セルフホストモード** (`BILLING_BACKEND=unlimited`): 全有償機能を
  解放。HTTP リクエストは発生させない。フォーク自家用運用を想定。

本体側コードに `import stripe` 等の決済プロバイダ SDK が現れたら
設計違反 (請求管理は billing コンテナに閉じ込める)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import wraps
from typing import Callable, Literal, Optional

from flask import abort, current_app
from flask_login import current_user


FeatureKey = Literal[
    "paid_llm",
    "voucher_storage",
    "timestamp_seal",
    "audit_seat",
    "auditor_plan_small",
    "auditor_plan_medium",
    "auditor_plan_unlimited",
]


class BillingClient(ABC):
    """エンタイトルメント問い合わせの抽象インターフェース。"""

    @abstractmethod
    def has_entitlement(self, user, feature_key: FeatureKey) -> bool:
        """ユーザーが指定機能を利用できるか。"""

    @abstractmethod
    def get_auditor_capacity(self, user) -> Optional[int]:
        """監査者プラン契約者の顧客数上限。

        - `None`: 無制限 (Unlimited プラン or セルフホスト)
        - `int`: 上限値 (Small=5, Medium=15)
        - `0`: 未契約
        """

    @abstractmethod
    def get_summary(self, user) -> dict:
        """設定画面の「現在のプラン」表示用サマリ。"""


class UnlimitedBillingClient(BillingClient):
    """セルフホスト向け実装。全機能を解放、HTTP リクエスト発行なし。"""

    def has_entitlement(self, user, feature_key: FeatureKey) -> bool:
        return True

    def get_auditor_capacity(self, user) -> Optional[int]:
        return None

    def get_summary(self, user) -> dict:
        return {
            "mode": "unlimited",
            "all_features_enabled": True,
            "auditor_capacity": None,
        }


def get_billing_client() -> BillingClient:
    """環境変数 `BILLING_BACKEND` に応じて適切な実装を返す。"""
    backend = current_app.config.get("BILLING_BACKEND", "unlimited")
    if backend == "unlimited":
        return UnlimitedBillingClient()
    if backend == "http":
        # Phase 3 で `HttpBillingClient` を実装次第、ここから import する。
        raise NotImplementedError(
            "BILLING_BACKEND='http' はまだ未実装です (Phase 3 #68 で対応)。"
        )
    raise RuntimeError(f"未知の BILLING_BACKEND: {backend!r}")


def has_entitlement(user, feature_key: FeatureKey) -> bool:
    """ユーザーが指定機能を利用できるか。"""
    return get_billing_client().has_entitlement(user, feature_key)


def get_auditor_capacity(user) -> Optional[int]:
    """監査者プランの顧客数上限。"""
    return get_billing_client().get_auditor_capacity(user)


def get_entitlement_summary(user) -> dict:
    """設定画面表示用のプラン概要。"""
    return get_billing_client().get_summary(user)


def require_entitlement(feature_key: FeatureKey) -> Callable:
    """View デコレータ: 指定機能の利用権がないリクエストを 403 でブロック。

    未認証は 401。`@login_required` と併用する場合は本デコレータが
    内側 (後で評価される位置) になるよう配置すること。
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if not has_entitlement(current_user, feature_key):
                abort(403)
            return func(*args, **kwargs)
        return wrapper
    return decorator
