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

**プリミティブ仕様 (byte 精度。client-py/TUI と相互実装するため確定値):**
- `Argon2id`: パラメータは**設計書 §4 (index.md) の確定値**を使う = `memory=64 MiB, iterations=3, parallelism=1`、出力 32B。`salt` は 16B (per-user、`wrapped_keys.salt` と同枠)。
- `HKDF` = **`HKDF-SHA256(ikm=master, salt=zero(32B), info=<上記文字列>, L=32)`**。info 文字列はバージョン付き (`iikanji-login-v1` / `iikanji-mk-wrap-v1`) を**全フローで厳守**する (短縮形を使わない)。`salt=zero(32B)` は RFC 5869 §2.2 (salt 省略時は HashLen バイトのゼロ列) に準拠し、既存 `bip39.js` の HKDF 実装と一致させる。

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
3. `login_verifier = HKDF(master,"iikanji-login-v1")`、`mk_wrap_key = HKDF(master,"iikanji-mk-wrap-v1")`。
4. クライアントが MK (32B random) を生成し `mk_wrap_key` で wrap。
5. サーバへ送信: `{salt, kdf_params, login_verifier, wrapped_master_key, wrap_iv}`。
   - サーバは `login_verifier` を**そのまま保存せず** `server_hash = HMAC-SHA256(LOGIN_SERVER_SECRET, login_verifier)`
     を保存する。`LOGIN_SERVER_SECRET` は**本用途専用の**環境変数管理サーバ固有秘密
     (email 擬名化の `server_secret` (index.md:67) とは**別変数**にして用途を混ぜない)。
     **DB が流出しても `LOGIN_SERVER_SECRET` が無ければ `login_verifier` 平文を得られない**
     (二重 slow KDF は不要。`login_verifier` は既に高エントロピーなので HMAC で十分)。
   - **`LOGIN_SERVER_SECRET` ローテーション**: 秘密を差し替えると全 `login_server_hash`
     が無効になるため、**遅延ローテーション**を採る = `login_server_hash` に
     `secret_version` を併記し、ログイン成功時 (= `login_verifier` を平文で握れる瞬間) に
     旧 version なら新 secret で `server_hash` を再計算して上書きする。強制再認証は不要。
     緊急失効が要る場合のみ全 version を無効化し全ユーザーにパスワード再設定を促す。
6. クライアントはリカバリシードを生成・表示し、別の wrapped_key として保存 (緊急用)。

### 3.2 ログイン (2 ラウンド)
1. `POST /auth/login/begin {username}` → サーバが `{salt, kdf_params}` を返す。
   未知ユーザーには**決定的ダミー salt** = `HMAC-SHA256(LOGIN_SERVER_SECRET, username)` から
   導いた 16B を返す (リクエスト毎にランダムだと「同名 2 回で salt が変わる」差で
   存在判定されるため、**username に対し決定的**にして列挙耐性を持たせる。脅威モデル
   §Q3 と整合)。
2. クライアント: `master = Argon2id(password, salt)`、`login_verifier = HKDF(master,"iikanji-login-v1")`。
3. `POST /auth/login/finish {username, login_verifier}` → サーバが
   `HMAC-SHA256(SERVER_SECRET, login_verifier)` と保存値を定数時間比較。OK ならセッション
   確立 + `wrapped_master_key` 等を返す。
4. クライアント: `mk_wrap_key = HKDF(master,"iikanji-mk-wrap-v1")` → MK unwrap → SharedWorker へ。
   **以後、別途の「暗号鍵解除」操作は不要**。

### 3.3 パスワード変更
- **必ず新 salt を生成する** (Argon2id の原則: 同パスワードでも salt が変われば
  `master` が変わり、過去に盗取された `login_verifier` を再利用できない)。
- 新 `password'` + 新 `salt'` → `master'` → 新 `login_verifier'` / `mk_wrap_key'`。
- **MK 自体は不変**。旧 `mk_wrap_key` で unwrap → 新 `mk_wrap_key'` で再 wrap。
- サーバへ `{new salt', new server_hash', new wrapped_master_key', new wrap_iv'}` を送る。
- 既存の `recovery_seed` / `passkey_prf` wrapped_key は MK 不変なので**そのまま有効**。

### 3.4 パスワード忘れ
- パスワードは MK の唯一の常用守り → 忘れたら**リカバリシードでのみ復旧**。
  リカバリで MK を unwrap → 新パスワードを設定し直す (3.3 と同じく再 wrap)。
- **リカバリシードは 1 回限り使用** (index.md §8 と整合)。復旧後に**旧シードを無効化し
  新シードを発行**して再提示する (使用済みシードの再利用を防ぐ)。
- リカバリシードも無ければ MK 復元不可能 (規約で明示。文言は `terms` テンプレートに
  追記する ToDo)。

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
   - **レート制限**: `/auth/login/begin` は**認証前**に呼ばれ列挙の攻撃面になるため、
     既存ログイン系と同等の `@limiter.limit("5-10/minute")` を**必ず付与**する
     (ダミー salt でも応答時間差での存在判定を避けるため定数時間で応答)。`/finish`
     も同様にレート制限。
   - **PR-3 までの過渡期**: PR-2 が先行マージされる間、`passkey_only_login` 制御は
     既存 `auth.py` の `/login` が引き続き担保する (新 `/begin`/`/finish` には §7.2 の
     パスワード必須化が PR-3 で入るまで passkey_only ユーザーを通さないガードを置く)。
3. **PR-3 ウィザード再構成**: 「パスワード = 鍵」前提に。初回登録でパスワードから
   MK 確立 + リカバリシードを**必須バックアップ**として提示。passphrase 単独方式は
   廃止 (= login password に統合)。Passkey/リカバリは追加・緊急として残す
   (鍵の追加・削除 UI は実装済)。**この PR で `index.md §2 / §10` の鍵管理記述を
   本方式に更新する** (`passphrase` method の意味変化を反映)。
4. **PR-4 パスワード変更/リセット**: MK 不変・ラップ更新。リカバリ経由リセット。
5. **PR-5 client-py / TUI**: 同じ派生を実装 (web と byte 互換)。
6. **ドキュメント**: 設計書 §1 (能動脅威の限界と native client 信頼根拠) / §2 / §10
   を本方式に更新。

## 7. 影響範囲・DB スキーマ差分

### 7.1 DB スキーマ
- `users.password_hash` (werkzeug hash) を**廃止**し、代わりに
  `users.login_server_hash` (= `HMAC-SHA256(LOGIN_SERVER_SECRET, login_verifier)`、32B) と
  `users.login_salt` (16B) + `users.login_kdf_params` (JSON) + `users.login_secret_version`
  (SMALLINT、§3.1 の遅延ローテーション用) を置く。(werkzeug 依存も撤去。)
- `wrapped_keys` テーブルの `method` enum は**変更しない** (`passkey_prf` /
  `passphrase` / `recovery_seed`)。`login` という新 method は**作らない** ——
  「login パスワード由来の鍵」は従来 `passphrase` method の wrapped_key として
  保存する (派生元がログインパスワードに変わるだけで、wrap の仕組みは同じ)。
  これにより鍵の追加・削除/解錠 UI を再利用できる。
  **注**: `index.md §2 / §10` は現状「パスフレーズ (フォールバック)」= 別途設定する
  passphrase として記述しており、本方式での意味変化 (= ログインパスワード由来) は
  **PR-3 で `index.md` を更新して反映**する (それまでは本設計書が正)。
- 破壊的変更可のため既存ユーザーはクリーン再作成 (段階移行不要)。

### 7.2 `passkey_only_login` ユーザーの扱い
- 現行 `auth.py` はパスワードログインを passkey_only ユーザーに対し弾く
  (`app/views/auth.py`)。本方式は「パスワード = 常用鍵」が前提なので衝突する。
- **方針 (確定)**: v5.x では **`passkey_only_login` を廃止**し、**全ユーザーに
  パスワード (= 常用鍵) を必須**とする。Passkey は §1 のとおり**任意の上乗せ**
  (Passkey PRF → `mk_wrap_key` 相当を別 wrapped_key として追加できる) に限定。
  - 理由: パスワード非保有だと「ログイン = MK 解錠」の単一入力 UX が成立せず、
    Passkey PRF の環境依存 (Bitwarden 非対応等) で常用解錠が不安定になるため。
  - v5.0 時点の passkey_only ユーザーが居れば、移行時にパスワード設定を必須化
    (破壊的変更可なのでクリーン再作成で吸収)。

### 7.3 その他
- `auth.py` (login 2 ラウンド化 / register / password 変更 / recovery)。
- API 認証 (Bearer トークン) は別系統で影響限定。
