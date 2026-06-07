from flask import Flask

from app.config import Config
from app.extensions import db, migrate, login_manager, csrf, limiter


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # Models (import so Alembic can detect them)
    from app import models  # noqa: F401

    # Blueprints
    from app.views import register_blueprints
    register_blueprints(app)

    # JSON API は CSRF 免除
    from app.views.webauthn import bp as webauthn_bp
    csrf.exempt(webauthn_bp)
    from app.views.api import bp as api_bp
    csrf.exempt(api_bp)
    # #385 ログイン派生 MK の 2 ラウンドログイン API も JSON 専用 (fetch) なので
    # CSRF 免除 (webauthn_bp / api_bp と同方針)。ログイン CSRF は本人の
    # パスワード/login_verifier を要するため有害な悪用にならない。
    from app.views.auth_api import bp as auth_api_bp
    csrf.exempt(auth_api_bp)
    # #385 PR-4b-2: リカバリシードによるパスワードリセット API も JSON 専用 (fetch)。
    # 未認証で叩くがリセットには旧 recovery_verifier の照合が要るため CSRF 免除でも
    # 有害な悪用にならない (auth_api_bp と同方針、§3.4.1)。
    from app.views.auth_recovery import bp as auth_recovery_bp
    csrf.exempt(auth_recovery_bp)
    # E2EE 鍵管理 API (E1 #108) も JSON 専用。PR-C で Bearer 対応する際に
    # 非ブラウザクライアントが CSRF トークンを得られない問題を避けるため、
    # 既存 api_bp / webauthn_bp と一貫して csrf.exempt する。
    # ⚠️ Bearer 対応 (PR-C) までの間、Web セッション認証のみで CSRF 免除と
    # いう構成上、悪意あるサイトから fetch + credentials:include で DELETE
    # 等を発火される可能性がある (id 推測難 + 最終鍵削除 409 ガードで
    # アカウントロックアウトは防止)。
    from app.views.wrapped_keys import bp as wrapped_keys_bp
    csrf.exempt(wrapped_keys_bp)
    # X25519 鍵ペア API も JSON 専用 (Bearer / セッション統合認証) なので CSRF 免除
    from app.views.keypair import bp as keypair_bp
    csrf.exempt(keypair_bp)
    # 監査連携 API (audit-packages / audit-responses) も JSON 専用なので CSRF 免除
    from app.views.audit_packages import bp as audit_packages_bp
    csrf.exempt(audit_packages_bp)
    # ai-config E2EE API も Bearer 認証 / JSON 専用なので CSRF 免除
    # (既存 api_bp / wrapped_keys_bp と同じ方針)
    from app.views.ai_config_api import bp as ai_config_api_bp
    csrf.exempt(ai_config_api_bp)
    # OAuth Device Flow のクライアント向けエンドポイントは CSRF 免除
    # (ブラウザ向けの authorize エンドポイントは CSRF 保護を維持)
    from app.views.oauth import device_authorization, token as oauth_token_view
    csrf.exempt(device_authorization)
    csrf.exempt(oauth_token_view)

    # リカバリコードでログイン後の強制復旧フロー
    # 「新パスキー登録 + リカバリコード再生成」両達成までは限定ページのみ許可
    @app.before_request
    def pending_recovery_gate():
        from flask import request, redirect, url_for, session
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return
        if not session.get("pending_recovery_action"):
            return
        # セッション整合性チェック: pending_recovery_user_id が現在のログイン
        # ユーザーと一致しない場合は flag を破棄（別ユーザーで再ログイン等）
        pending_uid = session.get("pending_recovery_user_id")
        if pending_uid is not None and pending_uid != cu.id:
            session.pop("pending_recovery_action", None)
            session.pop("pending_recovery_user_id", None)
            return
        endpoint = request.endpoint or ""
        # 強制復旧の遂行に必要なエンドポイントだけ許可
        allowed_endpoints = {
            "settings.passkeys",
            "settings.delete_passkey",
            "settings.recovery_generate",
            "webauthn.register_options",
            "webauthn.register_verify",
            "auth.logout",
            # 規約未同意のリカバリ後ユーザーが terms_acceptance_check と
            # 相互リダイレクトで無限ループに陥らないよう許可。同意してから
            # 復旧フロー (パスキー再登録 + リカバリ再生成) に進む順序。
            "auth.accept_terms",
            # 公開フォーム: 復旧中でも問い合わせ・法的文書閲覧を許可
            "legal.show",
            "legal.contact",
        }
        if endpoint in allowed_endpoints or endpoint.startswith("static"):
            return
        # E2EE 鍵管理 API は JSON。pending_recovery 中でも自分の wrapped_keys
        # を確認/更新できる必要がある (リカバリ後の鍵再設定フロー)。
        if (
            endpoint.startswith("wrapped_keys.")
            or endpoint.startswith("keypair.")
            or endpoint.startswith("audit_packages.")
        ):
            return
        return redirect(url_for("settings.passkeys"))

    # 鍵未設定ロックゲート (§16.5 / E7 #114 PR-4b)
    @app.before_request
    def migration_lock_gate():
        """`is_active=False` (鍵未設定ロック) のユーザーを、鍵設定または
        退会に必要なエンドポイント以外からブロックしてロック解決ページへ送る。

        ログイン時に `auth.login` が `force=True` で限定セッションを張るため、
        ロック中でも `current_user.is_authenticated` は True。ここで行動を
        ロック解決フローに限定する (`pending_recovery_gate` と同型)。

        自己回復: 鍵設定ウィザード完了で `users.public_key` が立つと、ここで
        `is_active=True` / `locked_at=NULL` に戻してロックを解く。鍵設定済み
        判定は migration-status CLI の `key_set` (public_key IS NOT NULL) と
        一致させている。前提: `is_active` は §16.5 の鍵未設定ロック専用
        (CLAUDE.md / 056 コメント)。将来 admin 停止等の別用途が増えたら、
        public_key 有無による自己回復の前提を見直すこと。
        """
        from flask import request, redirect, url_for
        from flask_login import current_user as cu
        if not cu.is_authenticated or cu.is_active:
            return
        if cu.public_key is not None:
            cu.is_active = True
            cu.locked_at = None
            db.session.commit()
            return
        endpoint = request.endpoint or ""
        # ロック解決の遂行に必要なエンドポイントだけ許可する。
        allowed_endpoints = {
            "migration_lock.locked",
            "settings.delete_account",
            "auth.logout",
        }
        if endpoint in allowed_endpoints or endpoint.startswith("static"):
            return
        # 鍵設定/解錠に必要な JSON API・Passkey 認証フロー・法的文書/問い合わせ。
        if (
            endpoint.startswith("wrapped_keys.")
            or endpoint.startswith("keypair.")
            or endpoint.startswith("webauthn.")
            or endpoint.startswith("legal.")
        ):
            return
        return redirect(url_for("migration_lock.locked"))

    # Before-request hook for terms acceptance check
    @app.before_request
    def terms_acceptance_check():
        """規約改訂時の再同意フロー (Phase 1 #66)。

        `User.accepted_terms_version` が `CURRENT_TERMS_VERSION` と一致しない
        認証済みユーザーを `/auth/accept-terms` に強制リダイレクトする。
        `CURRENT_TERMS_VERSION` が空文字なら同意管理は無効化 (テスト・
        セルフホスト用)。
        """
        from flask import request, redirect, url_for
        from flask_login import current_user as cu
        if not cu.is_authenticated:
            return
        # 鍵未設定ロック中 (§16.5) は migration_lock_gate が制御する。ここで
        # accept-terms へ誘導すると lock gate と相互リダイレクトで無限ループに
        # なるため、ロック中はスキップ (鍵設定 or 退会の後に規約再同意へ進む)。
        if not cu.is_active:
            return
        current_version = app.config.get("CURRENT_TERMS_VERSION", "")
        if not current_version:
            return
        if cu.accepted_terms_version == current_version:
            return
        endpoint = request.endpoint or ""
        # 同意画面自体・ログアウト・法的文書閲覧・静的アセット・
        # WebAuthn API は例外。`auth.recovery_login` は冒頭の
        # `is_authenticated` チェックで弾かれるためここに含めない。
        allowed = (
            "auth.accept_terms",
            "auth.logout",
        )
        if endpoint in allowed:
            return
        if (
            endpoint.startswith("static")
            or endpoint.startswith("legal.")
            or endpoint.startswith("webauthn.")
            or endpoint.startswith("api.")
            # OAuth デバイス認可フロー (TUI / MCP 等クライアント連携) は
            # 未同意でもブロックしない。クライアント側で「規約未同意のため
            # Web で同意が必要」と案内するのは難しいので、サーバー側で
            # オープンにしておく。Web UI 経由のアクセス時に同意フローへ
            # 誘導される設計。
            or endpoint.startswith("oauth.")
            # E2EE 鍵管理 API (E1 #108) は JSON クライアント。302 リダイレクト
            # ではなくクライアント側で規約同意状態を別途確認させる。
            or endpoint.startswith("wrapped_keys.")
            or endpoint.startswith("keypair.")
            or endpoint.startswith("audit_packages.")
        ):
            return
        # 元々アクセスしようとしていたパスに戻れるよう ?next= を引き継ぐ
        return redirect(url_for("auth.accept_terms", next=request.path))


    # Context processor for dev flag
    @app.context_processor
    def inject_dev_flag():
        return {"is_dev": app.debug}

    # #385: ログイン派生 MK の 2 ラウンドログインを有効化するか。LOGIN_SERVER_SECRET
    # 未設定なら login.html は従来の werkzeug フォーム送信のまま (フラグ off)。
    @app.context_processor
    def inject_login_derived_flag():
        return {"login_derived_enabled": bool(app.config.get("LOGIN_SERVER_SECRET"))}

    # Context processor for CAPTCHA
    @app.context_processor
    def inject_captcha_config():
        return {
            "captcha_provider": app.config.get("CAPTCHA_PROVIDER"),
            "captcha_site_key": app.config.get("CAPTCHA_SITE_KEY"),
            "captcha_api_url": app.config.get("CAPTCHA_API_URL"),
        }

    # Serve service worker from root scope
    @app.route("/sw.js")
    def service_worker():
        from flask import send_from_directory
        return send_from_directory(app.static_folder, "sw.js",
                                   mimetype="application/javascript")

    # Security headers
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not app.debug:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # CLI commands
    register_cli(app)

    return app


def register_cli(app):
    @app.cli.command("seed")
    def seed_command():
        """標準勘定科目区分を初期投入する"""
        from app.services.seed import seed_account_types
        seed_account_types()
        print("勘定科目区分を投入しました。")

    @app.cli.command("seed-user")
    def seed_user_command():
        """全ユーザーに標準勘定科目を投入する"""
        from app.models.user import User
        from app.services.seed import seed_accounts_for_user
        for user in User.query.all():
            seed_accounts_for_user(user.id)
            print(f"ユーザー {user.username} に標準科目を投入しました。")

    import click

    @app.cli.command("migrate-e2ee-data")
    @click.option("--user-id", type=int, default=None,
                  help="特定ユーザーのみ移行 (省略時は全ユーザー)")
    def migrate_e2ee_data_command(user_id):
        """E7 一斉移行: 平文台帳データを temp-MK でサーバ側暗号化する (#114)。

        alembic revision 054 (平文列 DROP 前) の状態で実行すること。仕訳・仕訳明細・
        医療費の平文を encrypted_blob へ暗号化し、以後の 055 以降のドロップを安全に
        通せるようにする。冪等。証憑画像・AI 下書き画像・クライアント再ラップは別途。
        """
        from app.services.e2ee_data_migration import migrate_all_to_e2ee
        totals = migrate_all_to_e2ee(db, user_id=user_id)
        # totals は暗号化件数 (整数) のみ。int() で明示キャストして件数だけを出力する
        # (CodeQL py/clear-text-logging の誤検知回避: mk を扱う関数の戻り値を CodeQL が
        # 機密と誤判定するため、整数化でサニタイズしてから表示)。
        summary = ", ".join(
            f"{k}={int(totals[k])}"
            for k in ("users", "journal_entries", "journal_entry_lines",
                      "medical_expenses", "vouchers", "voucher_audit_logs")
        )
        print("E2EE データ暗号化 完了: " + summary)

    @app.cli.command("migration-status")
    @click.option("--json", "as_json", is_flag=True,
                  help="JSON で出力 (機械処理用)")
    def migration_status_command(as_json):
        """E7 (#114) temp-MK 再ラップ移行の進捗を集計表示する (§16.6)。

        personal ユーザーについて、鍵設定状況と temp-MK 保持状況を件数で表示する。
        全員が temp-MK を保持しなくなれば (temp_mk_active=0)、運用者は移行が完遂した
        と判断し temp-MK 材料を破棄できる。read のみ・破壊操作なし。

        出力件数は全て整数で、temp-MK バイト列等の機密値は一切出力しない。
        """
        import json as _json
        from app.services.migration_status import compute_migration_counts

        c = compute_migration_counts()
        # 既存の CLI 出力契約 (PR-4a) を維持しつつ、集計は共有関数に委譲する。
        stats = {
            "total": c["total"],
            "key_set": c["with_keys"],
            "key_unset": c["without_keys"],
            "temp_mk_active": c["temp_mk_holders"],
            "locked": c["locked"],
            "safe_to_discard_temp_mk": c["temp_mk_holders"] == 0,
        }

        if as_json:
            print(_json.dumps(stats, ensure_ascii=False))
            return

        print("E2EE 移行進捗 (personal ユーザー)")
        print(f"  総ユーザー数            : {stats['total']}")
        print(f"  鍵設定済み              : {stats['key_set']}")
        print(f"  鍵未設定                : {stats['key_unset']}")
        print(f"  temp-MK 保持中(移行待ち) : {stats['temp_mk_active']}")
        print(f"  ロック中(is_active=False): {stats['locked']}")
        if stats["safe_to_discard_temp_mk"]:
            print(
                "\n[移行完遂] temp-MK を保持するユーザーはいません。"
                "temp-MK 材料を破棄できます。"
            )
        else:
            print(
                f"\n[移行待ち] {stats['temp_mk_active']} 名が temp-MK を保持中です。"
                "temp-MK 材料は破棄しないでください。"
            )

    @app.cli.command("migration-lock-stale")
    @click.option("--execute", is_flag=True,
                  help="実際にロックする (既定は dry-run で対象一覧のみ)")
    @click.option("--json", "as_json", is_flag=True,
                  help="JSON で出力 (機械処理用)")
    @click.option("--limit", type=int, default=None,
                  help="処理件数の上限 (段階適用・検証用)")
    def migration_lock_stale_command(execute, as_json, limit):
        """E7 (#114) §16.5: 猶予期間を過ぎた鍵未設定ユーザーをロックする。

        メンテナンスウィンドウ (config.MIGRATION_WINDOW_DATE) から
        MIGRATION_LOCK_GRACE_DAYS (既定 30) 日を過ぎても鍵 (public_key) を
        設定していない移行対象ユーザー (migration_temp_mk 保持中・personal・
        is_active=True) を `is_active=False` + `locked_at=now` にし、ロック解決
        フロー (鍵設定 or 退会) へ誘導する。次回ログイン時に migration_lock_gate
        が /migration/locked へ送る。

        既定は **dry-run** (対象一覧のみ)。`--execute` 明示時のみロックを適用し、
        対象ユーザーへ通知メールを送る。MIGRATION_WINDOW_DATE 未設定なら no-op。
        """
        import json as _json
        from datetime import datetime, timedelta, timezone, date as _date
        from app.models.user import User

        window_str = app.config.get("MIGRATION_WINDOW_DATE", "")
        if not window_str:
            print("MIGRATION_WINDOW_DATE が未設定のためロック処理をスキップしました。")
            return
        try:
            window = _date.fromisoformat(window_str)
        except ValueError:
            print(f"[error] MIGRATION_WINDOW_DATE の形式が不正です: {window_str!r} "
                  "(YYYY-MM-DD を指定してください)")
            return

        grace = app.config.get("MIGRATION_LOCK_GRACE_DAYS", 30)
        now = datetime.now(timezone.utc)
        cutoff = datetime.combine(
            window + timedelta(days=grace), datetime.min.time(),
            tzinfo=timezone.utc,
        )
        if now < cutoff:
            remaining = (cutoff - now).days
            print(f"猶予期間中です (基準日 {window} + {grace}日 = "
                  f"{cutoff.date()})。あと約 {remaining} 日。ロック対象なし。")
            if as_json:
                print(_json.dumps({"locked": 0, "cutoff": cutoff.date().isoformat(),
                                   "in_grace": True}, ensure_ascii=False))
            return

        # 移行対象 (temp-MK 保持中) かつ鍵未設定 (public_key NULL) の有効
        # personal ユーザー。temp-MK 条件で、移行コホート以外 (公開後の新規
        # 登録など temp-MK を持たないユーザー) を巻き込まないようスコープする。
        query = User.query.filter(
            User.user_type == "personal",
            User.is_active.is_(True),
            User.public_key.is_(None),
            User.migration_temp_mk.isnot(None),
        ).order_by(User.id)
        if limit:
            query = query.limit(limit)
        targets = query.all()

        if not execute:
            print(f"[dry-run] ロック対象: {len(targets)} 件 "
                  f"(基準日 {window} + {grace}日 = {cutoff.date()} を経過)")
            for u in targets:
                print(f"  [dry-run] {u.id}: {u.username} <{u.email}>")
            if as_json:
                print(_json.dumps({
                    "would_lock": len(targets),
                    "cutoff": cutoff.date().isoformat(),
                    "executed": False,
                }, ensure_ascii=False))
            return

        from app.services.mail import send_email
        locked = 0
        mail_failed = 0
        for u in targets:
            u.is_active = False
            u.locked_at = now
            locked += 1
        db.session.commit()
        # 通知メールはロック確定後に送る (送信失敗してもロックは維持)。
        for u in targets:
            if not u.email:
                continue
            try:
                send_email(
                    u.email,
                    "migration_locked",
                    {"username": u.username},
                    raise_on_send_error=True,
                )
            except Exception as e:
                print(f"  [warn] メール送信失敗 {u.id} <{u.email}>: {e}")
                mail_failed += 1

        print(f"ロック完了: {locked} 件 (メール送信失敗 {mail_failed} 件)")
        if as_json:
            print(_json.dumps({
                "locked": int(locked),
                "mail_failed": int(mail_failed),
                "cutoff": cutoff.date().isoformat(),
                "executed": True,
            }, ensure_ascii=False))

    @app.cli.command("migration-purge-locked")
    @click.option("--execute", is_flag=True,
                  help="実際に退会(削除)する (既定は dry-run で対象一覧のみ)")
    @click.option("--json", "as_json", is_flag=True,
                  help="JSON で出力 (機械処理用)")
    @click.option("--limit", type=int, default=None,
                  help="処理件数の上限 (段階適用・検証用)")
    def migration_purge_locked_command(execute, as_json, limit):
        """E7 (#114) §16.5: ロック後 60 日経過した鍵未設定ユーザーを自動退会する。

        `migration-lock-stale` でロック (is_active=False) されてから
        MIGRATION_PURGE_GRACE_DAYS (既定 60) 日を過ぎても鍵設定も退会も
        しなかったユーザー (public_key 未設定・locked_at が閾値より前) を
        `delete_user_account` で物理削除する (電帳法対象の VoucherAuditLog は
        匿名化保持)。**不可逆**。

        既定は **dry-run** (対象一覧のみ)。削除は `--execute` 明示時のみ実行する。
        JSON スキーマは全モード共通: matched / purged / failed / executed。
        """
        import json as _json
        from datetime import datetime, timedelta, timezone
        from app.models.user import User
        from app.services.account_deletion import delete_user_account

        grace = app.config.get("MIGRATION_PURGE_GRACE_DAYS", 60)
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(days=grace)

        query = User.query.filter(
            User.user_type == "personal",
            User.is_active.is_(False),
            User.public_key.is_(None),
            User.locked_at.isnot(None),
            User.locked_at < threshold,
        ).order_by(User.id)
        if limit:
            query = query.limit(limit)
        # 削除でオブジェクトが detach されるため、必要情報を先に確定する。
        targets = [(u.id, u.username, u.email) for u in query.all()]

        def _emit(purged, failed):
            print(f"対象 {len(targets)} 件 / 退会 {purged} 件 / 失敗 {failed} 件 "
                  f"(ロックから {grace} 日経過・{threshold.date()} 以前)")
            if as_json:
                print(_json.dumps({
                    "matched": len(targets),
                    "purged": int(purged),
                    "failed": int(failed),
                    "executed": bool(execute),
                }, ensure_ascii=False))

        if not execute:
            for uid, uname, email in targets:
                print(f"  [dry-run] {uid}: {uname} <{email}>")
            _emit(0, 0)
            return

        purged = 0
        failed = 0
        for uid, uname, email in targets:
            try:
                delete_user_account(uid)
                purged += 1
                print(f"  退会: {uid}: {uname}")
            except Exception as e:
                # 1 件の失敗で全体を止めない (delete_user_account は内部で
                # commit するため、成功分は確定済み)。
                db.session.rollback()
                print(f"  [warn] 退会失敗 {uid}: {uname}: {e}")
                failed += 1
        _emit(purged, failed)

    @app.cli.command("notify-terms-update")
    @click.option("--dry-run", is_flag=True, help="送信せず対象一覧のみ表示")
    @click.option("--limit", type=int, default=None, help="送信件数の上限")
    def notify_terms_update_command(dry_run, limit):
        """規約改訂通知メールを一括送信 (Phase 6 #71)。

        `accepted_terms_version` が `CURRENT_TERMS_VERSION` と一致しない
        メール登録済ユーザー全員にお知らせを送る。運用者が規約改訂
        後にトリガーする想定。
        """
        from flask import url_for
        from sqlalchemy import or_
        from app.models.user import User
        current_version = app.config.get("CURRENT_TERMS_VERSION", "")
        if not current_version:
            print("CURRENT_TERMS_VERSION が未設定のため通知をスキップしました。")
            return

        query = User.query.filter(
            User.email.is_not(None),
            User.email != "",
            or_(
                User.accepted_terms_version != current_version,
                User.accepted_terms_version.is_(None),
            ),
        ).order_by(User.id)
        if limit:
            query = query.limit(limit)
        targets = query.all()

        print(f"対象ユーザー: {len(targets)} 件 (現バージョン: {current_version})")
        if dry_run:
            for u in targets:
                cur = u.accepted_terms_version or "NULL"
                print(f"  [dry-run] {u.id}: {u.username} <{u.email}> (現: {cur})")
            return

        from app.services.mail import send_email
        sent = 0
        failed = 0
        if not app.config.get("SERVER_NAME"):
            print("[warn] SERVER_NAME 未設定: メール本文の URL が "
                  "http://localhost/... になります。")
        with app.test_request_context():
            terms_url = url_for("legal.show", slug="terms", _external=True)
            privacy_url = url_for("legal.show", slug="privacy", _external=True)
            for u in targets:
                try:
                    send_email(
                        u.email,
                        "terms_update",
                        {
                            "username": u.username,
                            "new_version": current_version,
                            "terms_url": terms_url,
                            "privacy_url": privacy_url,
                        },
                        raise_on_send_error=True,
                    )
                    sent += 1
                except Exception as e:
                    print(f"  [warn] {u.id} <{u.email}>: {e}")
                    failed += 1
        print(f"送信完了: 成功 {sent} 件 / 失敗 {failed} 件")

    @app.cli.command("generate-thumbnails")
    @click.option("--dry-run", is_flag=True, help="サムネイル生成をスキップして対象件数のみ表示")
    def generate_thumbnails_command(dry_run):
        """既存画像のサムネイルを一括生成する"""
        from app.models.voucher import Voucher
        from app.models.ai_draft import AIDraft
        from app.services.storage import (
            get_storage_backend, make_thumbnail_key, generate_thumbnail,
        )

        backend = get_storage_backend()
        generated = 0
        skipped = 0
        errors = 0

        # 論理削除済 (Phase 5 #70) は除外 (画像ファイルがもう存在しない)
        for v in Voucher.active().all():
            thumb_key = make_thumbnail_key(v.image_key)
            if backend.exists(thumb_key):
                skipped += 1
                continue
            if dry_run:
                generated += 1
                continue
            try:
                image_data = backend.get(v.image_key)
                thumb_bytes = generate_thumbnail(image_data)
                backend.put(thumb_key, thumb_bytes, "image/jpeg")
                generated += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR: Voucher {v.id}: {e}")

        for d in AIDraft.query.filter(AIDraft.status.in_(["analyzed"])).all():
            thumb_key = make_thumbnail_key(d.image_key)
            if backend.exists(thumb_key):
                skipped += 1
                continue
            if dry_run:
                generated += 1
                continue
            try:
                image_data = backend.get(d.image_key)
                thumb_bytes = generate_thumbnail(image_data)
                backend.put(thumb_key, thumb_bytes, "image/jpeg")
                generated += 1
            except Exception as e:
                errors += 1
                print(f"  ERROR: Draft {d.id}: {e}")

        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}サムネイル生成: {generated}件, スキップ: {skipped}件, エラー: {errors}件")

    @app.cli.command("invite-create")
    @click.argument("email")
    @click.option(
        "--user-type", default="personal",
        type=click.Choice(["personal", "auditor"]),
        help="招待先のアカウント種別",
    )
    @click.option(
        "--expires-in-days", default=7, type=int,
        help="招待トークンの有効期限 (日数)",
    )
    @click.option(
        "--no-email", is_flag=True,
        help="メール送信せずトークンを標準出力のみに表示",
    )
    def invite_create_command(email, user_type, expires_in_days, no_email):
        """招待トークンを発行してメール送信する (Phase 8 #72)。

        トークンの raw 値は SHA-256 ハッシュで保存されるため、メール送信時
        または `--no-email` で標準出力に出力したタイミングが raw を取得
        できる唯一の機会。
        """
        from app.models.invitation import InvitationToken
        from app.services.mail import send_email
        from flask import url_for

        raw, record = InvitationToken.generate(
            email, user_type=user_type,
            expires_in_days=expires_in_days,
        )
        db.session.add(record)
        db.session.commit()

        # register_url の組立て: REGISTRATION_INVITE_ONLY モードでは
        # user_type に応じて register / register_auditor を出し分け
        endpoint = "auth.register_auditor" if user_type == "auditor" else "auth.register"
        from werkzeug.routing import BuildError
        try:
            register_url = url_for(endpoint, token=raw, _external=True)
        except (RuntimeError, BuildError):
            # SERVER_NAME 未設定 (RuntimeError) や endpoint 未登録
            # (BuildError) のときのフォールバック。config の SERVER_NAME /
            # PREFERRED_URL_SCHEME を使って絶対 URL を組み立てる
            # (相対パスだとメール本文として無効)。
            from flask import current_app
            base = current_app.config.get("SERVER_NAME") or "localhost"
            scheme = current_app.config.get("PREFERRED_URL_SCHEME", "https")
            path = "/register/auditor" if user_type == "auditor" else "/register"
            register_url = f"{scheme}://{base}{path}?token={raw}"
            print(
                "[warn] SERVER_NAME が未設定のため URL が不完全な可能性があります。"
                " 環境変数で SERVER_NAME=your.host を設定してください。"
            )

        if not no_email:
            try:
                send_email(email, "invitation", {
                    "email": email,
                    "register_url": register_url,
                    "expires_at": record.expires_at.strftime(
                        "%Y-%m-%d %H:%M UTC"
                    ),
                    "service_label": (
                        "監査用アカウント" if user_type == "auditor"
                        else "個人アカウント"
                    ),
                })
                print(f"招待メールを送信しました: {email} ({user_type})")
            except Exception as e:
                print(f"メール送信失敗: {e}")
                print("以下の URL を手動で送信してください:")
                print(f"  {register_url}")
        else:
            print(f"招待トークン発行: {email} ({user_type})")
            print(f"  Register URL: {register_url}")
            print(
                f"  Expires at: "
                f"{record.expires_at.isoformat()}"
            )

    @app.cli.command("storage-audit")
    @click.option(
        "--fix", is_flag=True,
        help="StorageUsage の drift を実測値で上書き修正する",
    )
    def storage_audit_command(fix):
        """ストレージ整合性監査 (Phase 5 #70)。

        ``file_size`` NULL の Voucher / AIDraft をストレージから実測して
        埋め、``StorageUsage`` の集計値と実測合計の drift を検出する。
        ``--fix`` で drift を実測値に同期する。
        """
        from app.services.storage_audit import (
            audit_storage_usage, backfill_file_sizes,
        )

        print("=== file_size backfill ===")
        bf = backfill_file_sizes()
        print(
            f"Voucher backfilled: {bf['voucher_backfilled']}, "
            f"AIDraft backfilled: {bf['draft_backfilled']}, "
            f"Errors: {len(bf['errors'])}"
        )
        for e in bf["errors"][:10]:
            print(f"  ERROR: {e}")

        print()
        print("=== StorageUsage drift audit ===")
        au = audit_storage_usage(fix=fix)
        prefix = "[FIX] " if fix else "[DRY] "
        print(
            f"{prefix}Users checked: {au['users_checked']}, "
            f"Drift detected: {au['drift_detected']}, "
            f"Drift fixed: {au['drift_fixed']}"
        )
        for d in au["drifts"][:10]:
            print(
                f"  user={d['user_id']}: measured={d['measured']} "
                f"recorded={d['recorded']} delta={d['delta']:+d}"
            )

    @app.cli.command("rotate-cleanup")
    @click.option("--dry-run", is_flag=True, help="削除せず対象件数のみ表示")
    def rotate_cleanup_command(dry_run):
        """E2EE MK ローテーションの auto_abort_at 経過分を自動 abort する。

        設計書 §10.5 / §16.4: ローテーション中にデバイス紛失等で commit/abort
        いずれも飛んでこないケースを救済。cron / systemd timer で 1 時間ごとに
        起動する想定。
        """
        from datetime import datetime, timezone

        from app.models.user import User
        from app.models.wrapped_key import WrappedKey

        now = datetime.now(timezone.utc)
        users = (
            User.query
            .filter(User.mk_rotation_state.isnot(None))
            .all()
        )
        aborted = 0
        deleted_total = 0
        skipped = 0
        errors = 0
        for user in users:
            state = user.mk_rotation_state or {}
            if state.get("status") != "rotating":
                continue
            auto_abort_at = state.get("auto_abort_at")
            if not auto_abort_at:
                continue
            try:
                deadline = datetime.fromisoformat(auto_abort_at)
            except ValueError:
                deadline = None
            if deadline is None or deadline > now:
                skipped += 1
                continue
            # auto_abort をユーザー単位で実行・コミット (1 ユーザーの失敗を他に
            # 巻き込まないよう独立トランザクション)
            new_set = state.get("new_wrapped_keys_id_set", []) or []
            if dry_run:
                aborted += 1
                deleted_total += len(new_set)
                print(
                    f"  [dry-run] user_id={user.id} "
                    f"new_wrapped_keys={len(new_set)} "
                    f"deadline={auto_abort_at}"
                )
                continue
            try:
                if new_set:
                    deleted = (
                        WrappedKey.query
                        .filter_by(user_id=user.id)
                        .filter(WrappedKey.id.in_(new_set))
                        .delete(synchronize_session=False)
                    )
                    deleted_total += deleted
                user.mk_rotation_state = None
                db.session.commit()
                aborted += 1
            except Exception as exc:
                db.session.rollback()
                errors += 1
                print(f"  skip: user_id={user.id} error={exc}")
        print(
            f"rotate-cleanup: aborted={aborted}, "
            f"deleted_wrapped_keys={deleted_total}, "
            f"still_in_window={skipped}, errors={errors}"
        )

    @app.cli.command("audit-cleanup")
    @click.option("--dry-run", is_flag=True, help="削除せず対象件数のみ表示")
    def audit_cleanup_command(dry_run):
        """期限切れ (expires_at 経過) の AuditPackage を削除する (E5 #112, §14.8)。

        監査パッケージは 90 日 TTL で自動消滅させる。フォワードセクレシー
        (送信側 ephemeral 秘密鍵は破棄済) と併せ、サーバ侵害時の過去データ
        露出を時間で限定する。紐づく AuditResponse は FK の ON DELETE CASCADE
        で連動削除される。cron / systemd timer で 1 時間ごとに起動する想定。
        """
        from datetime import datetime, timezone

        from app.models.audit import AuditPackage

        now = datetime.now(timezone.utc)
        expired = AuditPackage.query.filter(AuditPackage.expires_at < now)
        if dry_run:
            print(f"[DRY RUN] audit-cleanup: 期限切れ AuditPackage={expired.count()} 件 (削除せず)")
            return
        deleted = expired.delete(synchronize_session=False)
        db.session.commit()
        print(f"audit-cleanup: deleted AuditPackage={deleted} 件 (AuditResponse は CASCADE 削除)")

    @app.cli.command("export-cleanup")
    @click.option("--dry-run", is_flag=True, help="削除せず対象件数のみ表示")
    def export_cleanup_command(dry_run):
        """期限切れ (expires_at 経過) の ExportJob を blob ごと削除する (E6 #113 §15.4 PR-3)。

        全データエクスポートのサーバ一時保存 (export_jobs, §15.4) は 24h TTL で
        自動消滅させる。期限切れジョブのストレージ blob (.ikexport) を先に削除し、
        次に DB 行を削除する。blob 削除失敗 (既に無い等) はログのみで続行する
        (storage.delete は冪等)。cron / systemd timer で 1 時間ごとに起動する想定。
        """
        from datetime import datetime, timezone

        from app.models.export_job import ExportJob
        from app.services.storage import get_storage_backend

        now = datetime.now(timezone.utc)
        expired = ExportJob.query.filter(ExportJob.expires_at < now).all()
        if dry_run:
            print(
                f"[DRY RUN] export-cleanup: 期限切れ ExportJob={len(expired)} 件 (削除せず)"
            )
            return

        backend = get_storage_backend()
        blob_deleted = 0
        for job in expired:
            if job.storage_key:
                try:
                    backend.delete(job.storage_key)
                    blob_deleted += 1
                except Exception:
                    app.logger.exception(
                        "export-cleanup: blob 削除失敗 key=%s", job.storage_key
                    )
            db.session.delete(job)
        db.session.commit()
        print(
            f"export-cleanup: deleted ExportJob={len(expired)} 件 "
            f"(blob={blob_deleted} 件)"
        )

    @app.cli.command("v5-migrate-cleanup")
    @click.option("--dry-run", is_flag=True, help="削除対象を表示するだけで実行しない (既定)")
    @click.option("--execute", is_flag=True, help="実際に削除を実行する")
    def v5_migrate_cleanup_command(dry_run, execute):
        """v5.0 E2EE 移行に伴う廃止データを掃除する (設計書 §15.3)。

        廃止 provider 'llama_cpp' の user_ai_configs 行を削除する。自家ホスト
        LLM は E2EE と両立しないため v5.0 で廃止しており、該当ユーザーは設定
        画面で OpenAI / Anthropic / Google などの BYOK プロバイダーに再登録
        する必要がある。

        webhook_configs テーブルと ai_drafts.discord_webhook_url /
        discord_message_id カラムの物理削除は専用マイグレーション (064 / 065)
        が `flask db upgrade` 時に行う。本コマンドは移行状況の可視化として
        それらの残存件数も併せて報告する (既に DROP 済みなら「該当なし」)。

        --execute を付けない限り削除は行わない (dry-run が既定)。
        """
        from sqlalchemy import inspect as sa_inspect, text

        from app.models.ai_config import UserAIConfig

        do_execute = execute and not dry_run

        # マイグレーション 064/065 適用後はテーブル/カラムが存在しないため、
        # 監査表示はスキーマの実在を確認してから集計する (前方互換)。
        insp = sa_inspect(db.engine)
        tables = set(insp.get_table_names())

        def _safe_scalar(sql):
            try:
                return db.session.execute(text(sql)).scalar() or 0
            except Exception:
                db.session.rollback()
                return None

        webhook_count = None
        if "webhook_configs" in tables:
            webhook_count = _safe_scalar("SELECT COUNT(*) FROM webhook_configs")

        discord_count = None
        if "ai_drafts" in tables:
            draft_cols = {c["name"] for c in insp.get_columns("ai_drafts")}
            if "discord_webhook_url" in draft_cols or "discord_message_id" in draft_cols:
                discord_count = _safe_scalar(
                    "SELECT COUNT(*) FROM ai_drafts "
                    "WHERE discord_webhook_url IS NOT NULL "
                    "OR discord_message_id IS NOT NULL"
                )

        llama_q = UserAIConfig.query.filter_by(provider="llama_cpp")
        llama_count = llama_q.count()

        def _fmt(count, drop_note):
            if count is None:
                return f"該当なし ({drop_note})"
            return f"{count} 件 ({drop_note})"

        prefix = "[dry-run] " if not do_execute else ""
        print(f"{prefix}v5-migrate-cleanup 対象:")
        print(f"  user_ai_configs provider='llama_cpp': {llama_count} 件 (本コマンドが削除)")
        print(f"  webhook_configs: {_fmt(webhook_count, 'マイグレーション 064 が DROP')}")
        print(f"  ai_drafts.discord_*: {_fmt(discord_count, 'マイグレーション 065 が DROP')}")

        if not do_execute:
            print("[dry-run] 削除は実行していません。実行するには --execute を付けてください。")
            return

        deleted = llama_q.delete(synchronize_session=False)
        db.session.commit()
        print(f"v5-migrate-cleanup: deleted user_ai_configs (llama_cpp)={deleted} 件")

    # ai-config-migration-status / ai-config-reset-migrate-key CLI は
    # Phase E2-b で Fernet 完全廃止に伴い削除。旧 Fernet データが残存して
    # いるユーザーは設定画面で API キーを再入力する必要がある。
