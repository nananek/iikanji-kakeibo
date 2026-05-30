"""平文の仕訳複写エンドポイント (/journal/create-api) が撤去されたことの確認。

元帳モーダルの複写はクライアント側 AES-GCM 暗号化 + POST /api/v1/journals/batch
に移行した (E2EE 平文 WRITE 停止)。旧平文エンドポイントは存在しないことを保証する。
"""

import json


class TestCreateApiRemoved:
    """POST /journal/create-api は撤去済み (404)。"""

    def test_create_api_returns_404(
        self, db, logged_in_client, user, accounts, account_types
    ):
        resp = logged_in_client.post(
            "/journal/create-api",
            data=json.dumps({
                "date": "2026-01-15",
                "description": "複写テスト",
                "lines": [
                    {"account_code": "5010", "debit_amount": 1000, "credit_amount": 0},
                    {"account_code": "1010", "debit_amount": 0, "credit_amount": 1000},
                ],
            }),
            content_type="application/json",
        )
        # ルート自体が存在しないため 404 / 405 のいずれか (POST は受け付けない)
        assert resp.status_code in (404, 405)
