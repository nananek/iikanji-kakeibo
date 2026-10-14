"""MCP (Model Context Protocol) リモートサーバー + OAuth メタデータ。

- ``/mcp``: Streamable HTTP トランスポート (JSON-RPC 2.0)。
- ``/.well-known/oauth-protected-resource``: RFC 9728 Protected Resource Metadata。
- ``/.well-known/oauth-authorization-server``: RFC 8414 AS メタデータ。

認証は ``Authorization: Bearer`` (API キー ``ik_*`` / OAuth トークン
``ikt_*``)。未認証時は ``WWW-Authenticate: Bearer resource_metadata=...``
を返し、MCP クライアントが認可サーバーを発見できるようにする。

ツールは REST API (``/api/v1``) と同等の操作を提供する。トークンに付与
されたスコープで利用可否を判定する (認可画面でユーザーが絞り込める)。
"""

import base64
import json

from flask import Blueprint, current_app, jsonify, request

from app.extensions import db
from app.services import api_ops
from app.services.api_ops import ApiError
from app.services.oauth_scopes import SCOPES

bp = Blueprint("mcp", __name__)

# MCP Streamable HTTP のプロトコルバージョン。
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

SERVER_NAME = "iikanji-kakeibo"
SERVER_VERSION = "1.0.0"

# JSON-RPC エラーコード
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def issuer_url() -> str:
    """本サーバーの issuer (スキーム://ホスト) を返す。Host 偽装に依存しない。"""
    from app.services.urls import external_base_url
    return external_base_url()


def resource_url() -> str:
    return issuer_url() + "/mcp"


# ---------------------------------------------------------------------------
# 認証 / レスポンスヘルパー
# ---------------------------------------------------------------------------


def _unauthorized(message: str = "認証が必要です。"):
    prm = issuer_url() + "/.well-known/oauth-protected-resource"
    resp = jsonify({"error": message})
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = (
        f'Bearer resource_metadata="{prm}"'
    )
    return resp


def _resolve_principal():
    from app.views.api import resolve_bearer_principal

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, _unauthorized()
    principal, err = resolve_bearer_principal()
    if err is not None:
        return None, _unauthorized("無効なトークンです。")
    return principal, None


# ---------------------------------------------------------------------------
# JSON-RPC ヘルパー
# ---------------------------------------------------------------------------


def _rpc_result(req_id, result):
    return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_error(req_id, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return jsonify({"jsonrpc": "2.0", "id": req_id, "error": err})


def _text_content(payload) -> dict:
    return {
        "type": "text",
        "text": json.dumps(payload, ensure_ascii=False, indent=2),
    }


def _tool_error(message: str) -> dict:
    return {"content": [_text_content({"error": message})], "isError": True}


# ---------------------------------------------------------------------------
# ツール実装 (REST API 相当)
# ---------------------------------------------------------------------------


def _t_list_journals(principal, args):
    return api_ops.list_journals(
        principal.user_id,
        page=args.get("page", 1),
        per_page=args.get("per_page", 20),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
    )


def _t_get_journal(principal, args):
    if "id" not in args:
        raise ApiError("id は必須です。")
    return api_ops.get_journal(principal.user_id, int(args["id"]))


def _t_create_journal(principal, args):
    from app.models.ai_draft import AIDraft
    from app.services.voucher import create_voucher_from_draft

    entry = api_ops.create_journal(principal.user_id, args)

    draft_id = args.get("draft_id")
    if draft_id:
        draft = AIDraft.query.filter_by(
            id=int(draft_id), user_id=principal.user_id, status="analyzed"
        ).first()
        if not draft:
            return {
                "ok": True,
                "id": entry.id,
                "entry_number": entry.entry_number,
                "warning": (
                    f"下書き(id={draft_id})が見つからないため証憑は紐付け"
                    "られませんでした。"
                ),
            }
        from app.views.api import _mark_draft_done

        _mark_draft_done(draft, entry.entry_number)
        create_voucher_from_draft(draft, entry.id)
        db.session.commit()

    return {"ok": True, "id": entry.id, "entry_number": entry.entry_number}


def _t_delete_journal(principal, args):
    if "id" not in args:
        raise ApiError("id は必須です。")
    api_ops.delete_journal(principal.user_id, int(args["id"]))
    return {"ok": True}


def _t_list_vouchers(principal, args):
    return api_ops.list_vouchers(
        principal.user_id,
        page=args.get("page", 1),
        per_page=args.get("per_page", 20),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
        search=args.get("search"),
        amount_from=args.get("amount_from"),
        amount_to=args.get("amount_to"),
    )


def _t_get_voucher_image(principal, args):
    if "id" not in args:
        raise ApiError("id は必須です。")
    data, mime = api_ops.get_voucher_image(
        principal.user_id, int(args["id"]), thumb=bool(args.get("thumb"))
    )
    return {
        "__mcp_content__": [
            {
                "type": "image",
                "data": base64.b64encode(data).decode("ascii"),
                "mimeType": mime,
            }
        ]
    }


def _t_list_accounts(principal, args):
    return api_ops.list_accounts(
        principal.user_id, include_inactive=bool(args.get("include_inactive"))
    )


def _t_list_ai_drafts(principal, args):
    return api_ops.list_ai_drafts(
        principal.user_id,
        status=args.get("status", "analyzed"),
        page=args.get("page", 1),
        per_page=args.get("per_page", 50),
    )


def _t_get_ai_draft(principal, args):
    if "id" not in args:
        raise ApiError("id は必須です。")
    return api_ops.get_ai_draft(principal.user_id, int(args["id"]))


def _t_analyze_receipt(principal, args):
    from app.views.api import _perform_analyze

    if not args.get("image_base64"):
        raise ApiError("image_base64 は必須です。")
    try:
        image_bytes = base64.b64decode(args["image_base64"], validate=True)
    except (ValueError, TypeError):
        raise ApiError("image_base64 のデコードに失敗しました。")
    draft, suggestions = _perform_analyze(
        principal.user_id,
        image_bytes,
        args.get("mime_type", "image/jpeg"),
        args.get("comment") or "",
        notify=bool(args.get("notify")),
    )
    return {"ok": True, "draft_id": draft.id, "suggestions": suggestions}


def _t_delete_ai_draft(principal, args):
    from app.views.api import _perform_delete_draft

    if "id" not in args:
        raise ApiError("id は必須です。")
    _perform_delete_draft(principal.user_id, int(args["id"]))
    return {"ok": True}


def _t_trial_balance(principal, args):
    return api_ops.trial_balance(
        principal.user_id,
        year=args.get("year"),
        period_from=args.get("period_from", 0),
        period_to=args.get("period_to", 15),
    )


def _t_income_statement(principal, args):
    return api_ops.income_statement(
        principal.user_id, year=args.get("year"), month=args.get("month")
    )


def _t_monthly_comparison(principal, args):
    return api_ops.monthly_comparison(principal.user_id, year=args.get("year"))


def _t_tax_summary(principal, args):
    return api_ops.tax_summary(principal.user_id, year=args.get("year"))


def _obj(properties, required=None):
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    schema["additionalProperties"] = False
    return schema


_JOURNAL_LINE_SCHEMA = _obj(
    {
        "account_code": {"type": "string", "description": "勘定科目コード"},
        "debit": {"type": "integer", "description": "借方金額"},
        "credit": {"type": "integer", "description": "貸方金額"},
        "description": {"type": "string"},
    },
    required=["account_code"],
)

# name, description, inputSchema, scope, write, handler
TOOLS = [
    {
        "name": "list_journals",
        "description": "仕訳の一覧を取得する。日付範囲で絞り込める。",
        "inputSchema": _obj({
            "page": {"type": "integer"},
            "per_page": {"type": "integer"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
        }),
        "scope": "journals:read",
        "write": False,
        "handler": _t_list_journals,
    },
    {
        "name": "get_journal",
        "description": "仕訳 1 件の詳細 (明細行・紐付く証憑) を取得する。",
        "inputSchema": _obj({"id": {"type": "integer"}}, required=["id"]),
        "scope": "journals:read",
        "write": False,
        "handler": _t_get_journal,
    },
    {
        "name": "create_journal",
        "description": "複式簿記の仕訳を起票する。lines の借方/貸方合計は一致させること。",
        "inputSchema": _obj({
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "description": {"type": "string", "description": "摘要"},
            "lines": {"type": "array", "items": _JOURNAL_LINE_SCHEMA},
            "source": {"type": "string"},
            "draft_id": {
                "type": "integer",
                "description": "AI 下書き ID (指定時は証憑を紐付けて下書きを消費)",
            },
        }, required=["date", "description", "lines"]),
        "scope": "journals:create",
        "write": True,
        "handler": _t_create_journal,
    },
    {
        "name": "delete_journal",
        "description": "仕訳を削除する。確定済み期間・提出ロックの仕訳は削除できない。",
        "inputSchema": _obj({"id": {"type": "integer"}}, required=["id"]),
        "scope": "journals:delete",
        "write": True,
        "handler": _t_delete_journal,
    },
    {
        "name": "list_vouchers",
        "description": "証憑 (レシート画像) の一覧を取得する。",
        "inputSchema": _obj({
            "page": {"type": "integer"},
            "per_page": {"type": "integer"},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "search": {"type": "string"},
            "amount_from": {"type": "integer"},
            "amount_to": {"type": "integer"},
        }),
        "scope": "journals:read",
        "write": False,
        "handler": _t_list_vouchers,
    },
    {
        "name": "get_voucher_image",
        "description": "証憑画像を返す。thumb=true でサムネイル。",
        "inputSchema": _obj({
            "id": {"type": "integer"},
            "thumb": {"type": "boolean"},
        }, required=["id"]),
        "scope": "journals:read",
        "write": False,
        "handler": _t_get_voucher_image,
    },
    {
        "name": "list_accounts",
        "description": "勘定科目の一覧 (コード・名称・区分) を取得する。",
        "inputSchema": _obj({
            "include_inactive": {"type": "boolean"},
        }),
        "scope": "accounts:read",
        "write": False,
        "handler": _t_list_accounts,
    },
    {
        "name": "list_ai_drafts",
        "description": "AI 証憑仕訳の下書き一覧を取得する。",
        "inputSchema": _obj({
            "status": {"type": "string", "enum": ["analyzed", "done", "all"]},
            "page": {"type": "integer"},
            "per_page": {"type": "integer"},
        }),
        "scope": "ai:analyze",
        "write": False,
        "handler": _t_list_ai_drafts,
    },
    {
        "name": "get_ai_draft",
        "description": "AI 下書きの詳細 (仕訳候補を含む) を取得する。",
        "inputSchema": _obj({"id": {"type": "integer"}}, required=["id"]),
        "scope": "ai:analyze",
        "write": False,
        "handler": _t_get_ai_draft,
    },
    {
        "name": "analyze_receipt",
        "description": (
            "領収書画像 (base64) を AI 解析して仕訳候補の下書きを作成する。"
        ),
        "inputSchema": _obj({
            "image_base64": {"type": "string"},
            "mime_type": {
                "type": "string",
                "enum": ["image/jpeg", "image/png", "image/webp", "image/gif"],
            },
            "comment": {"type": "string"},
            "notify": {"type": "boolean"},
        }, required=["image_base64"]),
        "scope": "ai:analyze",
        "write": True,
        "handler": _t_analyze_receipt,
    },
    {
        "name": "delete_ai_draft",
        "description": "AI 下書きを削除する。",
        "inputSchema": _obj({"id": {"type": "integer"}}, required=["id"]),
        "scope": "ai:analyze",
        "write": True,
        "handler": _t_delete_ai_draft,
    },
    {
        "name": "get_trial_balance",
        "description": "試算表を取得する。",
        "inputSchema": _obj({
            "year": {"type": "integer"},
            "period_from": {"type": "integer", "description": "0-16"},
            "period_to": {"type": "integer", "description": "0-16"},
        }),
        "scope": "reports:read",
        "write": False,
        "handler": _t_trial_balance,
    },
    {
        "name": "get_income_statement",
        "description": "損益計算書を取得する。month 省略時は年間。",
        "inputSchema": _obj({
            "year": {"type": "integer"},
            "month": {"type": "integer", "description": "1-12"},
        }),
        "scope": "reports:read",
        "write": False,
        "handler": _t_income_statement,
    },
    {
        "name": "get_monthly_comparison",
        "description": "月次比較 (費目別の月別推移) を取得する。",
        "inputSchema": _obj({"year": {"type": "integer"}}),
        "scope": "reports:read",
        "write": False,
        "handler": _t_monthly_comparison,
    },
    {
        "name": "get_tax_summary",
        "description": "確定申告集計 (税区分別) と医療費集計を取得する。",
        "inputSchema": _obj({"year": {"type": "integer"}}),
        "scope": "reports:read",
        "write": False,
        "handler": _t_tax_summary,
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def visible_tools(principal) -> list[dict]:
    """トークンのスコープで利用可能なツールのみ返す。"""
    return [t for t in TOOLS if principal.has_scope(t["scope"])]


def _tool_schema(t) -> dict:
    return {
        "name": t["name"],
        "description": t["description"],
        "inputSchema": t["inputSchema"],
    }


def _call_tool(tool: dict, principal, args) -> dict:
    if not principal.has_scope(tool["scope"]):
        return _tool_error(
            f"このトークンには {tool['scope']} 権限がありません。"
            "認可時に権限を絞り込んだ可能性があります。"
        )
    if tool["write"] and principal.read_only:
        return _tool_error("このトークンは読み取り専用です。")
    if not isinstance(args, dict):
        return _tool_error("arguments はオブジェクトで指定してください。")

    try:
        result = tool["handler"](principal, args)
    except ApiError as e:
        return _tool_error(e.message)
    except (ValueError, TypeError) as e:
        current_app.logger.warning(
            "Invalid arguments for MCP tool %s: %s", tool["name"], e
        )
        return _tool_error("引数が不正です。")
    except Exception:
        current_app.logger.exception("MCP tool %s failed", tool["name"])
        return _tool_error("ツールの実行に失敗しました。")

    if isinstance(result, dict) and "__mcp_content__" in result:
        return {"content": result["__mcp_content__"], "isError": False}
    if isinstance(result, dict) and "content" in result and "isError" in result:
        return result
    return {"content": [_text_content(result)], "isError": False}


# ---------------------------------------------------------------------------
# JSON-RPC ディスパッチ
# ---------------------------------------------------------------------------


def _handle_message(msg, principal):
    """1 つの JSON-RPC メッセージを処理する。

    Returns:
        (response_dict, is_notification)
    """
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": INVALID_REQUEST, "message": "Invalid Request"},
        }, False

    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        client_version = params.get("protocolVersion")
        version = (
            client_version if client_version in PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "いいかんじ™家計簿の家計データにアクセスする MCP サーバーです。"
                "試算表・損益計算書・確定申告集計の参照や、仕訳の起票・削除、"
                "AI 下書きの操作ができます。付与されたスコープの範囲のみ実行"
                "できます。"
            ),
        }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}, False

    if method in ("notifications/initialized", "notifications/cancelled",
                  "notifications/roots/list_changed"):
        return None, True

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}, False

    if method == "tools/list":
        tools = [_tool_schema(t) for t in visible_tools(principal)]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}, False

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            return {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": INVALID_PARAMS,
                          "message": f"未知のツールです: {name}"},
            }, False
        result = _call_tool(tool, principal, args)
        return {"jsonrpc": "2.0", "id": req_id, "result": result}, False

    if is_notification:
        # 未知の通知は黙って無視する (MCP の作法)。
        return None, True

    return {
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": METHOD_NOT_FOUND,
                  "message": f"Method not found: {method}"},
    }, False


@bp.route("/mcp", methods=["POST"])
def mcp_post():
    principal, err = _resolve_principal()
    if err is not None:
        return err

    # プロトコルバージョンヘッダーの検証 (設定されていれば)。
    hdr_version = request.headers.get("MCP-Protocol-Version")
    if hdr_version and hdr_version not in PROTOCOL_VERSIONS:
        return jsonify({"error": "Unsupported protocol version"}), 400

    # Accept ヘッダー: application/json か text/event-stream のいずれかが必要
    accept = request.headers.get("Accept", "")
    if accept and not (
        "application/json" in accept or "text/event-stream" in accept
        or "*/*" in accept
    ):
        return jsonify({"error": "Not Acceptable"}), 406

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return _rpc_error(None, PARSE_ERROR, "Parse error"), 400

    if isinstance(payload, list):
        # 2025-06-18 ではバッチは廃止。単一メッセージのみ受け付ける。
        return _rpc_error(
            None, INVALID_REQUEST, "Batch requests are not supported"
        ), 400
    if not isinstance(payload, dict):
        return _rpc_error(None, INVALID_REQUEST, "Invalid Request"), 400

    response, is_notification = _handle_message(payload, principal)
    if is_notification or response is None:
        return "", 202
    return jsonify(response)


@bp.route("/mcp", methods=["GET"])
def mcp_get():
    """サーバー起点 SSE は提供しない (MCP 仕様で 405 可)。"""
    return jsonify({"error": "Method Not Allowed"}), 405


@bp.route("/mcp", methods=["DELETE"])
def mcp_delete():
    """ステートレス実装のためセッション終了は no-op。"""
    return "", 204


@bp.route("/mcp", methods=["OPTIONS"])
def mcp_options():
    return "", 204


@bp.after_request
def _cors_headers(response):
    if request.path == "/mcp" or request.path.startswith("/.well-known/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id"
        )
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, POST, DELETE, OPTIONS"
        )
        response.headers["Access-Control-Expose-Headers"] = (
            "Mcp-Session-Id, WWW-Authenticate"
        )
    return response


# ---------------------------------------------------------------------------
# OAuth ディスカバリ (RFC 9728 / RFC 8414)
# ---------------------------------------------------------------------------


@bp.route(
    "/.well-known/oauth-protected-resource", methods=["GET"]
)
@bp.route(
    "/.well-known/oauth-protected-resource/mcp", methods=["GET"]
)
def protected_resource_metadata():
    issuer = issuer_url()
    return jsonify({
        "resource": issuer + "/mcp",
        "authorization_servers": [issuer],
        "scopes_supported": list(SCOPES.keys()),
        "bearer_methods_supported": ["header"],
        "resource_name": "いいかんじ™家計簿 MCP",
    })


@bp.route("/.well-known/oauth-authorization-server", methods=["GET"])
def authorization_server_metadata():
    issuer = issuer_url()
    return jsonify({
        "issuer": issuer,
        "authorization_endpoint": issuer + "/oauth/authorize",
        "token_endpoint": issuer + "/oauth/token",
        "registration_endpoint": issuer + "/oauth/register",
        "revocation_endpoint": issuer + "/oauth/revoke",
        "scopes_supported": list(SCOPES.keys()),
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:device_code",
        ],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "resource_indicators_supported": True,
        "service_documentation": issuer + "/docs/",
    })
