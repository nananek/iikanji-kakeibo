"""メール配信基盤 (Phase 6 #71)。

`BillingClient` と同じく抽象インターフェース + 複数実装の構造。
本ファイルは Phase 6 最初の PR の骨格部分で、`ConsoleMailBackend` のみ
を提供する。SMTP / SES / Resend 等の実プロバイダ実装と、個別の通知
(監査招待 / セキュリティアラート / クオータ警告 / 規約改訂等) の
呼び出し箇所組み込みは後続 PR で対応する。

設計方針:
- 呼び出し側はテンプレート名と context を渡すだけで配信プロバイダの
  詳細を意識しない (`send_email(to, template_name, context)`)
- 開発・テスト・セルフホスト運用 (`MAIL_BACKEND=console`) では
  標準出力にダンプするだけで実送信なし
- 公開 SaaS 運用では `MAIL_BACKEND=smtp` (or `ses` / `resend`) を指定し
  プロバイダ別設定を環境変数で渡す
- テンプレートは `templates/email/<name>.txt` (プレーン必須) と
  `templates/email/<name>.html` (任意) で組み立てる
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

from flask import current_app, render_template
from jinja2 import TemplateNotFound


logger = logging.getLogger(__name__)


@dataclass
class RenderedEmail:
    """テンプレートをレンダリングした結果。"""

    subject: str
    text_body: str
    html_body: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)


class MailBackend(ABC):
    """メール配信の抽象インターフェース。"""

    @abstractmethod
    def send(self, to: str, from_addr: str, rendered: RenderedEmail) -> None:
        """送信する。失敗時は例外を送出する。"""


class ConsoleMailBackend(MailBackend):
    """標準出力にダンプするだけのバックエンド。

    開発・テスト・セルフホスト運用 (`MAIL_BACKEND=console`) の既定値。
    実送信は発生しない。
    """

    def send(self, to: str, from_addr: str, rendered: RenderedEmail) -> None:
        sep = "=" * 60
        print(sep, file=sys.stdout)
        print(f"[Mail] From: {from_addr}", file=sys.stdout)
        print(f"[Mail] To:   {to}", file=sys.stdout)
        print(f"[Mail] Subject: {rendered.subject}", file=sys.stdout)
        for key, value in rendered.headers.items():
            print(f"[Mail] {key}: {value}", file=sys.stdout)
        print("-" * 60, file=sys.stdout)
        print(rendered.text_body, file=sys.stdout)
        if rendered.html_body:
            print("-" * 60 + " (HTML)", file=sys.stdout)
            print(rendered.html_body, file=sys.stdout)
        print(sep, file=sys.stdout, flush=True)


class SmtpMailBackend(MailBackend):
    """SMTP プロトコルで実送信するバックエンド。

    公開 SaaS 運用向け。STARTTLS / SMTPS 両モード、PLAIN 認証を扱う。
    接続は send() 呼出ごとに確立して即切断するシンプルな実装 (本数
    が増えたら接続プール化を検討)。

    必要な config:
    - `MAIL_SMTP_HOST`, `MAIL_SMTP_PORT`
    - `MAIL_SMTP_USERNAME`, `MAIL_SMTP_PASSWORD` (任意、空なら認証なし)
    - `MAIL_SMTP_USE_TLS`: ``"starttls"`` (587 推奨) / ``"ssl"`` (465) /
      ``"none"`` (平文、開発環境のみ)
    - `MAIL_SMTP_TIMEOUT`: 接続タイムアウト秒 (デフォルト 30)
    """

    def send(self, to: str, from_addr: str, rendered: RenderedEmail) -> None:
        cfg = current_app.config
        host = cfg.get("MAIL_SMTP_HOST")
        # MAIL_SMTP_PORT / MAIL_SMTP_TIMEOUT は config.py で int 化済のため
        # ここでの int() 二重変換は不要。`or` フォールバックも config.py が
        # default 値を持つため到達しない (dead code) が、防御的に残す。
        port = cfg.get("MAIL_SMTP_PORT") or 587
        username = cfg.get("MAIL_SMTP_USERNAME") or ""
        password = cfg.get("MAIL_SMTP_PASSWORD") or ""
        use_tls = (cfg.get("MAIL_SMTP_USE_TLS") or "starttls").lower()
        timeout = cfg.get("MAIL_SMTP_TIMEOUT") or 30
        if not host:
            raise RuntimeError(
                "MAIL_SMTP_HOST が未設定です (MAIL_BACKEND=smtp 時は必須)。"
            )

        message = EmailMessage()
        # `formataddr` で整形済の From は raw 文字列として渡す
        # (EmailMessage は内部で再エンコードしない)。
        message["From"] = from_addr
        message["To"] = to
        message["Subject"] = rendered.subject
        message["Message-ID"] = make_msgid()
        for key, value in rendered.headers.items():
            message[key] = value
        message.set_content(rendered.text_body, charset="utf-8")
        if rendered.html_body:
            message.add_alternative(
                rendered.html_body, subtype="html", charset="utf-8",
            )

        context = ssl.create_default_context()
        if use_tls == "ssl":
            with smtplib.SMTP_SSL(
                host, port, timeout=timeout, context=context,
            ) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                smtp.ehlo()
                if use_tls == "starttls":
                    smtp.starttls(context=context)
                    smtp.ehlo()
                elif use_tls != "none":
                    raise RuntimeError(
                        f"MAIL_SMTP_USE_TLS={use_tls!r} は無効です "
                        "(starttls / ssl / none のいずれかを指定)。"
                    )
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)


def get_mail_backend() -> MailBackend:
    """環境変数 `MAIL_BACKEND` に応じて実装を返す。

    将来 SES / Resend 等を追加する際はここに分岐を増やす。後で接続
    プール保持型を実装する場合は `lru_cache` / Flask `g` でリクエスト
    単位キャッシュを検討すること。
    """
    backend = current_app.config.get("MAIL_BACKEND", "console")
    if backend == "console":
        return ConsoleMailBackend()
    if backend == "smtp":
        return SmtpMailBackend()
    # 他のバックエンド (ses / resend 等) は後続 PR で実装する。
    raise NotImplementedError(
        f"MAIL_BACKEND={backend!r} はまだ未実装です (Phase 6 後続 PR で対応)。"
    )


def render_email(template_name: str, context: dict) -> RenderedEmail:
    """テンプレートをレンダリングする。

    - `templates/email/<template_name>/subject.txt` から件名 (1 行)
    - `templates/email/<template_name>/body.txt` からプレーン本文 (必須)
    - `templates/email/<template_name>/body.html` から HTML 本文 (任意)

    件名はメールヘッダ (Subject) に直接渡るため、CR / LF を含むユーザー
    入力がテンプレ展開された場合に **ヘッダインジェクションを許してしまう**
    リスクがある。`render_email` 全ての出口でサニタイズし、入口側 (フォーム
    バリデーション) と多重防御する。
    """
    subject_raw = render_template(
        f"email/{template_name}/subject.txt", **context
    )
    # \r / \n をスペースに置換 + 連続スペースを 1 つに圧縮 + strip
    subject = " ".join(subject_raw.replace("\r", " ").replace("\n", " ").split())
    text_body = render_template(f"email/{template_name}/body.txt", **context)
    html_body: Optional[str] = None
    try:
        html_body = render_template(f"email/{template_name}/body.html", **context)
    except TemplateNotFound:
        # HTML 版が無いテンプレートはプレーンのみで送信する。
        pass
    return RenderedEmail(subject=subject, text_body=text_body, html_body=html_body)


def send_email(
    to: str,
    template_name: str,
    context: Optional[dict] = None,
    *,
    raise_on_send_error: bool = False,
) -> None:
    """テンプレート名と context を指定してメール送信する。

    送信先 (`to`) はメールアドレス文字列。複数宛先や CC/BCC は本骨格では
    サポートしない (必要になった時点で API を拡張する)。

    例外スコープ:
    - `render_email` の失敗 (テンプレート未存在・context 不足等の
      プログラマーエラー) は **吸収せず呼び出し側に伝播** させる。
      早期発見が望ましいクラスのバグなので。
    - `backend.send` の失敗 (SMTP 接続エラー・送信プロバイダ側の障害等)
      は既定では **吸収してログだけ残す**。Web 経由の同期送信で本体
      フローに失敗を波及させないため。CLI バッチ (例: `flask
      notify-terms-update`) など、運用者が失敗件数を集計する用途では
      `raise_on_send_error=True` を渡すと例外が再 raise される。
    """
    if context is None:
        context = {}
    rendered = render_email(template_name, context)
    from_addr = _format_from_address(
        current_app.config.get("MAIL_FROM", ""),
        current_app.config.get("MAIL_FROM_NAME", ""),
    )
    backend = get_mail_backend()
    try:
        backend.send(to, from_addr, rendered)
    except Exception:
        logger.exception("Failed to send email '%s' to %s", template_name, to)
        if raise_on_send_error:
            raise


def _format_from_address(addr: str, name: str) -> str:
    """`Name <addr>` 形式 (RFC 5322) に整形する。

    非 ASCII の Display Name は `email.utils.formataddr` が自動的に
    RFC 2047 encoded-word (Base64, charset=utf-8) に変換するため、
    日本語の運営者名 (例: "いいかんじ™家計簿") もそのまま渡せる。
    SMTP ヘッダ互換性を確保。

    Subject 等のヘッダも将来 `SmtpMailBackend` を実装する際は同様に
    `email.header.Header` でエンコードすること。
    """
    if not addr and not name:
        return ""
    if not addr:
        # Display Name 単独の利用は SMTP 仕様的には変則的だが、
        # 既存の挙動を維持してテストとの整合を保つ。
        return name
    return formataddr((name or "", addr), charset="utf-8")
