"""エンタイトルメント (有償機能ゲート) の本体側ロジック。

本体は支払い状況の永続データを持たない。`BillingClient` を経由して
billing サービス (Phase 3 で別コンテナとして実装) に照会する設計。

公開ロードマップ Epic #64 / Phase 2 #67 のアーキテクチャ方針:

- **公開 SaaS 正式運用** (`BILLING_BACKEND=http`): billing コンテナに
  HTTP 照会。実装は Phase 3 で `HttpBillingClient` として追加予定。
- **セルフホスト全機能解放** (`BILLING_BACKEND=unlimited`): 全有償機能
  を解放。HTTP リクエストは発生させない。フォーク自家用運用 / 内部
  利用 / 検証用途を想定 (デフォルト)。
- **無償機能のみ** (`BILLING_BACKEND=free_only`): 全有償機能を拒否し
  ベース機能のみ提供する。billing コンテナを立てずに公開ベータを
  始めたい運用者向け。誰も `paid_llm` / `voucher_storage` /
  `audit_seat` 等にアクセスできない。

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
    def get_entitlement_summary(self, user) -> dict:
        """設定画面の「現在のプラン」表示用サマリ。"""


class UnlimitedBillingClient(BillingClient):
    """セルフホスト向け実装。全機能を解放、HTTP リクエスト発行なし。"""

    def has_entitlement(self, user, feature_key: FeatureKey) -> bool:
        return True

    def get_auditor_capacity(self, user) -> Optional[int]:
        return None

    def get_entitlement_summary(self, user) -> dict:
        return {
            "mode": "unlimited",
            "all_features_enabled": True,
            "auditor_capacity": None,
        }


class FreeOnlyBillingClient(BillingClient):
    """無償機能のみモード。billing コンテナがない環境向け実装。

    全 `feature_key` で False を返すため、`paid_llm` (自家ホスト LLM) /
    `voucher_storage` (証憑画像の永続保管) / `audit_seat` (監査枠) /
    `timestamp_seal` (TSA) 等の有償機能は誰も使えない。基本の家計簿機能
    (出納帳・仕訳・取込・レポート・お問い合わせ・退会等) のみ提供する。

    課金開始前の招待制ベータや、有償化方針が固まっていない段階で公開
    運用を始めるときに使う。billing コンテナの稼働は不要。
    """

    def has_entitlement(self, user, feature_key: FeatureKey) -> bool:
        return False

    def get_auditor_capacity(self, user) -> Optional[int]:
        return 0  # 未契約扱い

    def get_entitlement_summary(self, user) -> dict:
        return {
            "mode": "free_only",
            "all_features_enabled": False,
            "auditor_capacity": 0,
        }


def get_billing_client() -> BillingClient:
    """環境変数 `BILLING_BACKEND` に応じて適切な実装を返す。

    - `unlimited` (default): 全機能解放 (セルフホスト想定)
    - `free_only`: 全有償機能を拒否 (公開ベータ運用)
    - `http`: billing コンテナ参照 (Phase 3 で実装予定)

    現状は呼出しごとに新規インスタンスを生成する。`UnlimitedBillingClient`
    / `FreeOnlyBillingClient` はステートレスかつ軽量なので問題ないが、
    Phase 3 で `HttpBillingClient` (HTTP コネクションプール保持) を
    実装する際は `lru_cache(maxsize=1)` または Flask の `g` /
    `current_app` 拡張オブジェクト経由で再利用する形に変更すること。
    """
    backend = current_app.config.get("BILLING_BACKEND", "unlimited")
    if backend == "unlimited":
        return UnlimitedBillingClient()
    if backend == "free_only":
        return FreeOnlyBillingClient()
    if backend == "http":
        # Phase 3 で `HttpBillingClient` を実装次第、ここから import する。
        # その際 `feature_key` を外部 API に渡す前に
        # `typing.get_args(FeatureKey)` でランタイム検証を入れること。
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
    return get_billing_client().get_entitlement_summary(user)


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
