#!/usr/bin/env python3
"""AI API モックサーバー（E2E テスト用）

OpenAI 互換の /v1/chat/completions エンドポイントを提供。
Docker コンテナ内で実行される。

Usage:
    python mock-ai-server.py [--port PORT] [--cash-code CODE] [--food-code CODE]
"""
import argparse
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=11435)
    p.add_argument("--cash-code", type=str, default="1010")
    p.add_argument("--food-code", type=str, default="5010")
    return p.parse_args()


ARGS = None


def round1_response():
    return {
        "date": "2026-01-15",
        "description": "テスト商店",
        "amount": 1500,
        "document_type": "receipt",
        "items": [{"name": "テスト商品", "amount": 1500}],
        "needs_ledger": False,
        "requested_accounts": [],
    }


def csv_column_detect_response():
    """CSV列マッピングAI自動検出のレスポンス"""
    return {
        "date_col": 0,
        "desc_col": 1,
        "deposit_col": 2,
        "withdrawal_col": 3,
        "date_format": "%Y/%m/%d",
    }


def round2_response():
    return {
        "suggestions": [
            {
                "title": "食費として計上",
                "description": "コンビニでの購入と推定",
                "date": "2026-01-15",
                "entry_description": "テスト商店",
                "lines": [
                    {
                        "account_code": ARGS.food_code,
                        "account_name": "食費",
                        "debit_amount": 1500,
                        "credit_amount": 0,
                    },
                    {
                        "account_code": ARGS.cash_code,
                        "account_name": "現金",
                        "debit_amount": 0,
                        "credit_amount": 1500,
                    },
                ],
            }
        ]
    }


def wrap_openai(data):
    return {
        "id": "mock-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(data, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
    }


def wrap_openai_tool_call(tool_name, tool_args):
    return {
        "id": "mock-tool-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_mock_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args, ensure_ascii=False) if isinstance(tool_args, dict) else tool_args,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def wrap_responses_tool_call(tool_name, tool_args):
    return {
        "id": "resp-mock-1",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_mock_1",
                "name": tool_name,
                "arguments": json.dumps(tool_args, ensure_ascii=False) if isinstance(tool_args, dict) else tool_args,
            }
        ],
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path in ("/v1/chat/completions", "/v1/responses"):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length > 0 else "{}"

            has_tools = False
            is_round2 = False
            is_csv_detect = False
            try:
                parsed = json.loads(body)
                if parsed.get("tools"):
                    has_tools = True
                for msg in parsed.get("messages", []) if "messages" in parsed else parsed.get("input", []):
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                        )
                    if "列マッピングを推定" in content:
                        is_csv_detect = True
                        break
                    if "勘定科目一覧" in content:
                        is_round2 = True
                        break
            except json.JSONDecodeError:
                pass

            if has_tools:
                if self.path == "/v1/responses":
                    res_obj = wrap_responses_tool_call("submit_journal_suggestions", round2_response())
                else:
                    res_obj = wrap_openai_tool_call("submit_journal_suggestions", round2_response())
                label = "Agent Tool Call"
            elif is_csv_detect:
                res_obj = wrap_openai(csv_column_detect_response())
                label = "CSV Detect"
            elif is_round2:
                res_obj = wrap_openai(round2_response())
                label = "Round 2"
            else:
                res_obj = wrap_openai(round1_response())
                label = "Round 1"

            response = json.dumps(res_obj, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.encode())
            print(f"[mock-ai] {label} response sent", flush=True)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress default access logs


if __name__ == "__main__":
    ARGS = parse_args()
    server = HTTPServer(("0.0.0.0", ARGS.port), Handler)
    print(
        f"[mock-ai] Server running on port {ARGS.port} "
        f"(cash={ARGS.cash_code}, food={ARGS.food_code})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
