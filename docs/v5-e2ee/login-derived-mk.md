# ログインパスワード由来 MK 設計 (単一パスワードで E2EE)

設計書 §2 / §10 の鍵管理を、**ログインパスワードを唯一の常用鍵**として再構成する
ための設計。現行 (v5.0) の「passphrase / passkey / recovery を 3 方式並列で初回
選択」は UX が悪く、特にリカバリシードを第一級の選択肢に出したのは設計ミスだった。

> **前提**: 運用環境はいつでも v4.0.0-beta に戻せるため**破壊的変更可**。既存
> werkzeug ハッシュからの段階移行は不要で、クリーンカットオーバーしてよい。

## 1. 方針

| 方式 | 位置づけ |
|------|----------|
| **ログインパスワード** | **唯一の常用鍵**。ログイン = MK 解錠を 1 回の入力で同時に行う。 |
| **リカバリシード (BIP-39 24 語)** | **緊急バックアップ専用**。パスワード忘れ / 新端末初回用。日常解錠の選択肢にはしない (画面では「必ず控える保管物」として提示)。 |
| **Passkey (WebAuthn PRF)** | **任意の上乗せ**。対応環境のみ。無くても困らない (Bitwarden 等は PRF 非対応 / iOS は Safari 18+)。 |

目標: **ログインパスワードを MK にも流用してよい**。流用しても**受動的なサーバー
管理者 (DB とログを読むだけ) は MK を導けない**こと。

## 2. 核心: パスワードから派生を分岐 (HKDF split)

パスワードの生値をサーバーに**送らない**。クライアントで 1 本の slow KDF を回し、
用途ごとに HKDF で分離する。

```
(ログイン時、クライアント内のみ)
master         = Argon2id(login_password, salt)      # salt はログイン前にサーバから取得 (秘密でない)
login_verifier = HKDF(master, info="iikanji-login-v1")    # ← これだけサーバへ送り照合
mk_wrap_key    = HKDF(master, info="iikanji-mk-wrap-v1")   # ← サーバに送らない。MK を unwrap
```

- サーバが見るのは `login_verifier` だけ。HKDF は一方向なので `master` も
  `mk_wrap_key` も導けない → **パスワード流用前提でも受動管理者は MK を取れない**。
- `salt` と Argon2id パラメータはサーバが保持するが、それで MK を得るには結局
  `login_password` を**総当たり**するしかない (= 「流用によるただ取り」が消える)。
- ログイン成功時、クライアントは `master` を握っているので即 `mk_wrap_key` →
  MK unwrap。**ログイン 1 回で MK まで解錠**。

### HKDF info (ドメイン分離) 一覧

| 用途 | info |
|------|------|
| ログイン検証値 | `iikanji-login-v1` |
| MK ラップ鍵 | `iikanji-mk-wrap-v1` |

将来用途を足す場合は必ず別 info にする (PRF/HKDF のドメイン分離規約 §webauthn_prf と同様)。

## 3. フロー

### 3.1 登録 (パスワード設定)
1. クライアントが `salt` (16B random) を生成。
2. `master = Argon2id(password, salt)`。
3. `login_verifier = HKDF(master,"login")`、`mk_wrap_key = HKDF(master,"mk-wrap")`。
4. クライアントが MK (32B random) を生成し `mk_wrap_key` で wrap。
5. サーバへ送信: `{salt, kdf_params, server_hash(login_verifier), wrapped_master_key, wrap_iv}`。
   - サーバは `login_verifier` を**さらにサーバ側ハッシュ**して保存 (DB 流出時に
     `login_verifier` 平文が漏れないように。例: HMAC or scrypt)。
6. クライアントはリカバリシードを生成・表示し、別の wrapped_key として保存 (緊急用)。

### 3.2 ログイン (2 ラウンド)
1. `POST /auth/login/begin {username}` → サーバが `{salt, kdf_params}` を返す
   (未知ユーザーには**ダミー salt** を返し列挙耐性を持たせる)。
2. クライアント: `master = Argon2id(password, salt)`、`login_verifier = HKDF(master,"login")`。
3. `POST /auth/login/finish {username, login_verifier}` → サーバが
   `server_hash` 照合。OK ならセッション確立 + `wrapped_master_key` 等を返す。
4. クライアント: `mk_wrap_key = HKDF(master,"mk-wrap")` → MK unwrap → SharedWorker へ。
   **以後、別途の「暗号鍵解除」操作は不要**。

### 3.3 パスワード変更
- 新 `password'` → `master'` → 新 `login_verifier'` / `mk_wrap_key'`。
- **MK 自体は不変**。旧 `mk_wrap_key` で unwrap → 新 `mk_wrap_key'` で再 wrap。
- サーバへ `{new salt?, new server_hash, new wrapped_master_key}` を送る。
- 既存の `recovery_seed` / `passkey_prf` wrapped_key は MK 不変なので**そのまま有効**。

### 3.4 パスワード忘れ
- パスワードは MK の唯一の常用守り → 忘れたら**リカバリシードでのみ復旧**。
  リカバリで MK を unwrap → 新パスワードを設定し直す (3.3 と同じく再 wrap)。
- リカバリシードも無ければ MK 復元不可能 (規約で明示)。

## 4. 限界 (正直な明記)

1. **オフライン総当たりは原理的に残る**: サーバは `wrapped_master_key`+`salt` を
   持つので弱いパスワードは常に総当たり可能。これはパスワードで鍵を守る方式
   すべてに共通。OPAQUE でもこの「MK ラップへのオフライン攻撃」は消えない →
   よって OPAQUE の厳密さより **HKDF split で十分** (コスト対効果)。強いパスワード
   前提は不変 (8 文字以上、推奨 16 文字以上)。
2. **★能動的攻撃 (web の本質的限界)**: web はサーバが配信した JS が動くので、
   悪意ある管理者が**配信コードを改ざん**すれば生パスワードも MK も盗める。
   HKDF split は**受動管理者**には有効だが**能動管理者**には web では無力。
   完全な保証は **`client-py` / ネイティブ / SRI 固定の独立配布**を信頼根拠に
   据えることで得る (設計書 §1 脅威モデルに追記)。
3. **ユーザー列挙**: `login/begin` が salt を返すため、未知ユーザーにダミー salt
   を返す等の対策が要る。厳密にやるなら OPAQUE (RFC 9807)。

## 5. OPAQUE 採用可否

OPAQUE (aPAKE) を使えば「サーバは salt 相当も見ない + 列挙耐性」まで厳密化できる
が、(a) §4-1 の MK オフライン攻撃は OPAQUE でも消えない、(b) 実装が複雑で vetted
ライブラリ依存、という理由で **第一段では HKDF split を採用**。将来、認証強化が
必要になれば OPAQUE への置換を検討 (login_verifier 層だけ差し替え可能な設計に
しておく)。

## 6. 段階 PR 案

1. **PR-1 クライアント KDF プリミティブ**: `master/login_verifier/mk_wrap_key` 派生
   (argon2.js + HKDF) と単体テスト (golden vector)。
2. **PR-2 ログイン 2 ラウンド API**: `/auth/login/begin` `/finish` + サーバ側
   `server_hash` 保管。列挙耐性 (ダミー salt)。既存 `/login` は破壊的に置換。
3. **PR-3 ウィザード再構成**: 「パスワード = 鍵」前提に。初回登録でパスワードから
   MK 確立 + リカバリシードを**必須バックアップ**として提示。passphrase 単独方式は
   廃止 (= login password に統合)。Passkey/リカバリは追加・緊急として残す
   (鍵の追加・削除 UI は実装済)。
4. **PR-4 パスワード変更/リセット**: MK 不変・ラップ更新。リカバリ経由リセット。
5. **PR-5 client-py / TUI**: 同じ派生を実装 (web と byte 互換)。
6. **ドキュメント**: 設計書 §1 (能動脅威の限界と native client 信頼根拠) / §2 / §10
   を本方式に更新。

## 7. 影響範囲メモ

- `auth.py` (login/register/recovery)、`werkzeug` パスワードハッシュ廃止、
  `users.password_hash` → `login_verifier` 保管列へ。
- API 認証 (Bearer トークン) は別系統で影響限定。
- `passkey_only_login` モードはパスワード非保有なので、その場合は MK の常用解錠を
  Passkey/リカバリに委ねる (要 UX 整理)。
- 破壊的変更可のため既存ユーザーのクリーン再作成でよい (段階移行不要)。
