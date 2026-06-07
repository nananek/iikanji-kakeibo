# ログインパスワード由来 MK 設計 (単一パスワードで E2EE)

設計書 §2 / §10 の鍵管理を、**ログインパスワードを唯一の常用鍵**として再構成する
ための設計。現行 (v5.0) の「passphrase / passkey / recovery を 3 方式並列で初回
選択」は UX が悪く、特にリカバリシードを第一級の選択肢に出したのは設計ミスだった。

> **前提 (自然移行)**: 既存 v4 ユーザーは**アカウント・データを保持したまま初回ログイン時に
> 透過移行**する (クリーン再作成はしない)。移行するのは**認証因子のみ** (werkzeug パスワード →
> ログイン派生 MK)。データの暗号化自体は E3〜E7 の temp-MK 土台で**既に完了している** (v5
> デプロイ時に `e2ee_data_migration.py` が全平文をサーバ側 temp-MK で暗号化し平文列は物理 DROP
> 済)。本方式が初回ログイン時に行うのは ① werkzeug の最終 1 回検証 ② ログインパスワードからの
> MK 確立 ③ temp-MK→自分の MK への **rewrap の透過駆動** (既存 `/migration/rewrap` を再利用)
> の 3 点。ユーザーは「鍵設定」を意識せず、パスワード 1 回入力 + 進捗バーで完了する。**従来の
> gate + 3 方式ウィザードによる移行誘導は廃止**する (§7 参照)。

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
- `Argon2id`: パラメータは `memory=64 MiB, iterations=3, parallelism=1`、出力 32B。`salt` は 16B (per-user、`wrapped_keys.salt` と同枠)。**これを v5.x の確定値とする** (client-py/TUI と byte 互換が要るため可変にしない)。`index.md §4` 現行の「※調整余地あり」表記は本方式の実装 PR (PR-1) で**削除し確定する**。
- `HKDF` = **`HKDF-SHA256(ikm=master, salt=zero(32B), info=<上記文字列>, L=32)`**。info 文字列はバージョン付き (`iikanji-login-v1` / `iikanji-mk-wrap-v1`) を**全フローで厳守**する (短縮形を使わない)。**info は ASCII/UTF-8 でエンコードした bytes として扱う** (Python `b"iikanji-login-v1"`, JS `new TextEncoder().encode("iikanji-login-v1")`)。client-py/TUI との byte 互換に直結するため実装間で揃える。`salt=zero(32B)` を選ぶ**理由**: `master` は Argon2id 出力で高エントロピーなため、RFC 5869 §2.2 のゼロ salt 使用条件 (IKM が既に高エントロピーなら salt 省略可) を満たす。結果として既存 `bip39.js` の HKDF 実装とも一致する (= salt 値を変える理由がない。「bip39.js に合わせるため」ではなく、両者が同じ RFC 条件に従うから一致する)。
- **`seed_bytes` 正規化 (リカバリシード由来の HKDF 入力。§3.4.1 で使用)**: BIP-39 ニーモニックを
  ①Unicode **NFKD** 正規化 ②前後空白除去 (trim) ③連続空白を単一 ASCII space (0x20) に畳む
  ④小文字化 (lowercase) ⑤UTF-8 エンコード、の順でバイト列化する。`bip39.js` の
  `deriveKeyFromMnemonic` 入力と byte 一致させること (client-py/TUI も同手順で実装)。この
  `seed_bytes` を IKM に `info="iikanji-master-key-v1"` (MK unwrap 鍵) と
  `info="iikanji-recovery-login-v1"` (recovery_verifier) を別々に HKDF 導出する。
- **通信前提**: 上記すべて **TLS 必須**。`login_verifier` は HKDF で一方向化済みとはいえ照合値であり、平文 HTTP で送ると MITM が再送・なりすましに使えるため、登録/ログインの全 API は HTTPS のみで提供する。

- サーバが見るのは `login_verifier` だけ。HKDF は一方向なので `master` も
  `mk_wrap_key` も導けない → **パスワード流用前提でも受動管理者は MK を取れない**。
- `salt` と Argon2id パラメータはサーバが保持するが、それで MK を得るには結局
  `login_password` を**総当たり**するしかない (= 「流用によるただ取り」が消える)。
- ログイン成功時、クライアントは `master` を握っているので即 `mk_wrap_key` →
  MK unwrap。**ログイン 1 回で MK まで解錠**。

### HKDF info (ドメイン分離) 一覧

| 用途 | info | 導出元 (ikm) |
|------|------|------|
| ログイン検証値 | `iikanji-login-v1` | `master` (Argon2id(login_password)) |
| MK ラップ鍵 | `iikanji-mk-wrap-v1` | `master` (Argon2id(login_password)) |
| MK unwrap 鍵 (リカバリシード) | `iikanji-master-key-v1` | `seed_bytes` (BIP-39 24 語) |
| シード検証値 (recovery_verifier) | `iikanji-recovery-login-v1` | `seed_bytes` (BIP-39 24 語) |
| TOTP secret の at-rest 暗号鍵 (§3.6) | `iikanji-totp-enc-v1` | `LOGIN_SERVER_SECRET` |

将来用途を足す場合は必ず別 info にする (PRF/HKDF のドメイン分離規約 §webauthn_prf と同様)。
`iikanji-master-key-v1` は既存 (リカバリシードからの MK unwrap)、`iikanji-recovery-login-v1` は
§3.4.1 で新規追加 (同一シードから別 info で recovery_verifier を独立導出)。
注: `iikanji-master-key-v1` は `webauthn_prf.js` の WebAuthn PRF path でも同一文字列を info として
使用するが、IKM (一方はリカバリシード bytes、他方は PRF 出力) が異なるためドメイン分離は維持される
(出力衝突なし)。

### LOGIN_SERVER_SECRET の用途分離

`LOGIN_SERVER_SECRET` は HMAC の鍵として**2 用途**で使う。同一鍵の無分離使用を避け、
HMAC の**メッセージ先頭にドメインラベル + `0x00`** を付けて分離する。

| 用途 | HMAC メッセージ |
|------|----------------|
| ログイン検証ハッシュ | `"login-hash" \|\| 0x00 \|\| login_verifier` |
| 列挙耐性ダミー salt | `"dummy-salt" \|\| 0x00 \|\| username` |
| リカバリ検証ハッシュ (§3.4.1) | `"recovery-hash" \|\| 0x00 \|\| recovery_verifier` |
| リカバリダミー wrap (§3.4.1) | `"dummy-recovery-wrap" \|\| 0x00 \|\| username` |
| リカバリダミー wrap2 — 伸長用 (§3.4.1) | `"dummy-recovery-wrap2" \|\| 0x00 \|\| username` |
| リカバリダミー IV (§3.4.1) | `"dummy-recovery-iv" \|\| 0x00 \|\| username` |

(別環境変数に割るより 1 秘密 + ラベル分離の方が運用・ローテーションが単純。)
`secret_version` 機構 (§3.1): `recovery_seed_server_hash` も `login_server_hash` と**同一の
`LOGIN_SERVER_SECRET` + 同一 `login_secret_version`** を共有する (別カラムは設けない)。秘密
ローテ時は recovery reset / login 成功で平文 verifier を握れた瞬間に両ハッシュとも遅延再計算
する。リカバリダミー応答 (上記 3 ラベル) は秘密ローテで値が変わるが、決定的かつ列挙耐性のみが
目的なので version 管理は不要 (照合に使わない)。

## 3. フロー

### 3.1 登録 (パスワード設定)
1. クライアントが `salt` (16B random) を生成。
2. `master = Argon2id(password, salt)`。
3. `login_verifier = HKDF(master,"iikanji-login-v1")`、`mk_wrap_key = HKDF(master,"iikanji-mk-wrap-v1")`。
4. クライアントが MK (32B random) を生成し `mk_wrap_key` で wrap:
   **`AES-256-GCM(key=mk_wrap_key, iv=wrap_iv(12B random), plaintext=MK)`**。
   出力 = ciphertext(32B) + GCM tag(16B) = 48B を `wrapped_master_key`、`wrap_iv`(12B)
   を併せて保存する (index.md §5 と一致。§5 の「暫定」表記は本方式確定時に外す)。
5. サーバへ送信: `{salt, kdf_params, login_verifier, wrapped_master_key, wrap_iv}`。
   - サーバは `login_verifier` を**そのまま保存せず**
     `server_hash = HMAC-SHA256(LOGIN_SERVER_SECRET, "login-hash" || 0x00 || login_verifier)`
     を保存する。`LOGIN_SERVER_SECRET` は**本用途専用の**環境変数管理サーバ固有秘密
     (email 擬名化の `server_secret` (index.md:67) とは**別変数**にして用途を混ぜない)。
     **DB が流出しても `LOGIN_SERVER_SECRET` が無ければ `login_verifier` 平文を得られない**
     (二重 slow KDF は不要。`login_verifier` は既に高エントロピーなので HMAC で十分)。
     `"login-hash" || 0x00 || ...` のプレフィックスは**ドメイン分離**用 (§3.2 のダミー
     salt 用途と同一鍵を無分離で使わないため。下記「LOGIN_SERVER_SECRET の用途分離」)。
   - **`LOGIN_SERVER_SECRET` ローテーション**: 秘密を差し替えると全 `login_server_hash`
     が無効になるため、**遅延ローテーション**を採る = `login_server_hash` に
     `secret_version` を併記し、ログイン成功時 (= `login_verifier` を平文で握れる瞬間) に
     旧 version なら新 secret で `server_hash` を再計算して上書きする。強制再認証は不要。
     緊急失効が要る場合のみ全 version を無効化し全ユーザーにパスワード再設定を促す。
6. クライアントはリカバリシードを生成・表示し、別の wrapped_key として保存 (緊急用)。

### 3.2 ログイン (2 ラウンド)
1. `POST /auth/login/begin {username}` → サーバが `{salt, kdf_params, migration_required}`
   を返す。`migration_required` は**初回移行が必要か**のフラグ:
   - **既移行ユーザー** (`login_salt` 有) → `{salt: login_salt, kdf_params, migration_required: false}`。
   - **v4 移行対象** (`password_hash` 有 / `login_salt` 無) → `{salt: 新規ランダム 16B,
     kdf_params: 確定値, migration_required: true}`。**旧 werkzeug ハッシュから salt を導出しない**
     (新 salt をサーバ側で発行し、§3.5 の移行 finish で正式保存)。
   - **パスワード非保有 / 不正状態** (`password_hash` 無 / `login_salt` 無 = passkey_only
     ユーザー、または race 等で両列 NULL の異常状態) → `{salt: 新規ランダム 16B, kdf_params,
     migration_required: true, requires_password_setup: true}`。クライアントは**パスワード設定
     UI** (§3.1 登録フロー相当) を先に提示し、ユーザーがパスワードを設定してから §3.5 ③ の
     finish に進む。`login_verifier` 単独照合 (通常パス) には**絶対に入れない** (照合先
     `login_server_hash` が無く認証不能のため)。
   - **未知ユーザー** → **決定的ダミー salt** =
     `HMAC-SHA256(LOGIN_SERVER_SECRET, "dummy-salt" || 0x00 || username)[0:16]` から
     導いた 16B + `migration_required: false` を返す (リクエスト毎にランダムだと「同名 2 回で
     salt が変わる」差で存在判定されるため、**username に対し決定的**にして列挙耐性を持たせる。
     脅威モデル §Q3 と整合)。`migration_required` の真偽でも存在判定されぬよう、未知ユーザーへは
     常に `false` を返し、応答は定数時間にする。
2. クライアント: `master = Argon2id(password, salt)`、`login_verifier = HKDF(master,"iikanji-login-v1")`。
3. `POST /auth/login/finish {username, login_verifier}` → サーバが
   `HMAC-SHA256(LOGIN_SERVER_SECRET, "login-hash" || 0x00 || login_verifier)` を計算し
   保存値 (`login_server_hash`) と**定数時間比較** (実装は Python stdlib `hmac.compare_digest()`)。
   OK ならセッション確立 + `wrapped_master_key` 等を返す。
4. クライアント: `mk_wrap_key = HKDF(master,"iikanji-mk-wrap-v1")` → MK unwrap → SharedWorker へ。
   **以後、別途の「暗号鍵解除」操作は不要**。
   - **セッション内ライフタイム**: MK unwrap 完了後、`master` / `mk_wrap_key` は即座にゼロ化・
     破棄する (SharedWorker・メインスレッド双方に保持し続けない。Spectre 系サイドチャネルの
     露出面を最小化)。パスワード変更 (§3.3) 時は再導出が要るため、その場でユーザーに再入力を
     求めて導出し直す (保持はしない)。SharedWorker に常駐させるのは MK のみ。

### 3.3 パスワード変更
- **必ず新 salt を生成する** (Argon2id の原則: 同パスワードでも salt が変われば
  `master` が変わり、過去に盗取された `login_verifier` を再利用できない)。
- 新 `password'` + 新 `salt'` → `master'` → 新 `login_verifier'` / `mk_wrap_key'`。
- **MK 自体は不変**。旧 `mk_wrap_key` で unwrap → 新 `mk_wrap_key'` で再 wrap。
- サーバへ `{new salt', new server_hash', new wrapped_master_key', new wrap_iv'}` を送る。
- 既存の `recovery_seed` / `passkey_prf` wrapped_key は MK 不変なので**そのまま有効**。
- **(PR-4b-1 で適用)** パスワード変更でも旧パスワードで確立済みの既存セッションを失効させるため、
  §3.4.1 で導入する `session_token_version` を本フローの成功時にもインクリメントする。

### 3.4 パスワード忘れ
- パスワードは MK の唯一の常用守り → 忘れたら**リカバリシードでのみ復旧**。
  リカバリで MK を unwrap → 新パスワードを設定し直す (3.3 と同じく再 wrap)。
- **リカバリシードは 1 回限り使用** (index.md §8 と整合)。復旧後に**旧シードを無効化し
  新シードを発行**して再提示する (使用済みシードの再利用を防ぐ)。**無効化のタイミングは
  新 `wrapped_master_key` (新パスワード由来) のサーバ保存が完了した後**にする — MK unwrap 成功
  直後に旧シードを無効化すると、新パスワード設定が失敗した場合に MK へアクセスできなくなり詰む
  ため (順序: 新 wrap 保存完了 → 旧シード無効化 → 新シード提示)。**新シードの再提示は
  初回登録時と同等のセキュア表示要件**を満たすこと (1 回限り表示・画面/履歴キャッシュ
  禁止・コピー時の注意喚起。PR-4 で実装漏れしやすいので明記)。
- リカバリシードも無ければ MK 復元不可能 (規約で明示。文言は `terms` テンプレートに
  追記する ToDo)。

#### 3.4.1 リセット認証モデル: リカバリシードを「フル復旧因子」化 (採用方針)

パスワードを忘れたユーザーが確実に持つのは**ウィザードで必須提示したリカバリシード
(24 語)**。一方サーバが検証できる既存因子は recovery code (`ikr_`、ログイン用) のみで、
これは登録時に必ず作られるわけではない。よって**リセットはシード単体で完結できる**必要が
ある。シードは MK を unwrap できる (recovery_seed wrapped_key) が、**サーバ側 verifier が
無い**ため、シードを知らない攻撃者でも別 MK で `login_*`/wrapped_key を上書きしてアカウントを
破壊できる DoS 経路になる。これを塞ぐため、**リカバリシードにサーバ側 verifier を持たせて
ログイン因子にも昇格**する。

**ドメイン分離 (シードの 2 用途)**:
- MK unwrap 鍵 (既存): `HKDF-SHA256(seed_bytes, salt=zero(32), info="iikanji-master-key-v1", L=32)`
- リカバリ検証値 (新規): `recovery_verifier = HKDF-SHA256(seed_bytes, salt=zero(32),
  info="iikanji-recovery-login-v1", L=32)` (**出力長 32B**、`login_verifier` と同じ)。
  `salt=zero(32)` は**意図的** — `seed_bytes` (BIP-39 24 語, ~256 bit エントロピー) が IKM として
  十分なので可変 salt を導入しても安全性は向上しない (RFC 5869 §2.2、§2 の login HKDF と同方針)。
- `seed_bytes` の正規化はバイト精度で確定する (client-py/TUI が独自実装するため §2 のプリミティブ
  仕様に転記)。手順: ①Unicode NFKD 正規化 ②前後空白除去 (trim) ③連続空白を単一 ASCII space
  (0x20) に畳む ④小文字化 (lowercase) ⑤UTF-8 エンコード。これは `bip39.js` の
  `deriveKeyFromMnemonic` 入力と byte 一致させること。info 文字列が異なるので 2 値は独立。

**DB**: `users.recovery_seed_server_hash` (BYTEA 32B, nullable) =
`HMAC-SHA256(LOGIN_SERVER_SECRET, "recovery-hash" || 0x00 || recovery_verifier)`。
DB 流出時もシード平文/recovery_verifier を得られない (login_server_hash と同方針)。
recovery_seed wrapped_key を作成 (ウィザード) する際、クライアントが `recovery_verifier` も
計算して送り、サーバが本ハッシュを保存する。

**CSRF / 認証方針**: `/auth/recovery/begin` `/auth/recovery/finish` は**未認証の公開 JSON API**
(`/auth/login/begin|finish` と同じ扱い)。ログイン API 同様 **CSRF 免除** (`csrf.exempt`) とし、
代わりに `@limiter.limit` でレート制限する (login API と同方針 §3.2)。HTML フォーム POST では
なく `fetch` JSON で実装する。

**リセットフロー**:
1. 公開ページ `/auth/recovery-reset`。`POST /auth/recovery/begin {username}` →
   recovery_seed wrapped_key の `{wrapped_master_key, wrap_iv}` を返す (MK unwrap 用)。
   **列挙耐性 (§3.2 の dummy-salt と同方針)**: ユーザー不在 / `recovery_seed_server_hash` が
   NULL / recovery_seed wrapped_key 無しの全ケースで、**username 由来の決定的ダミー**を一様・
   定数時間で返す。**実値の長さと完全一致させる**こと: 実 `wrapped_master_key` は AES-256-GCM
   出力 = ciphertext 32B + GCM tag 16B = **48B**、`wrap_iv` は **12B**。ダミーも同じ長さで返す:
   ```
   dummy_wrap_raw = HMAC-SHA256(LOGIN_SERVER_SECRET, "dummy-recovery-wrap"  || 0x00 || username)      # 32B
   dummy_wrap_ext = HMAC-SHA256(LOGIN_SERVER_SECRET, "dummy-recovery-wrap2" || 0x00 || username)[0:16] # 16B
   wrapped_master_key = dummy_wrap_raw || dummy_wrap_ext                                               # 48B
   wrap_iv            = HMAC-SHA256(LOGIN_SERVER_SECRET, "dummy-recovery-iv" || 0x00 || username)[0:12] # 12B
   ```
   決定的なので「同一 username で 2 回叩いて値が変わる差」からシード設定有無が漏れず、長さも実値
   (48B/12B) と一致するので**長さによる real/dummy 判別もできない**。ダミーで unwrap しても finish
   の verifier 照合で必ず失敗する。

   **定数時間の実装指針 (タイミング攻撃対策 / 必須)**: 「定数時間」を満たすには、素直な分岐
   (実ユーザーのみ DB lookup → 実値 / 不在は lookup せずダミー) が DB I/O レイテンシ差 (1〜5ms)
   でユーザー存在を漏らすため**不可**。よって:
   - **DB lookup は分岐に関わらず常に 1 回実行**する (`username` で `User` を引く。不在でも
     SELECT を投げる)。
   - **ダミー HMAC 計算 (3 本) も常に実行**する。実値が使えるか否かは計算後に選択する
     (早期 return で計算をスキップしない)。
   - 実値返却 / ダミー返却の選択は**値の差し替えのみ**で行い、コードパスの分岐量を揃える。
   - PR-4b-2 のテストに「begin は DB hit/miss・NULL hash・wrapped_key 欠如のいずれでも常に同じ
     HMAC 計算回数を通る」ことの検証ケースを必ず含める。
   - **移行期の非対称状態** (WARN-1) もすべてダミー応答で通す: ①`recovery_seed_server_hash` は
     有るが recovery_seed wrapped_key が NULL、②wrapped_key は有るが `recovery_seed_server_hash`
     が NULL (旧ウィザードで作成したユーザー)。どちらも begin はダミーで通し、finish の verifier
     照合で失敗させる (NULL hash は照合不能なので必ず失敗)。移行期テストにこの 2 状態を含める。
2. ユーザーが 24 語シード入力。クライアント: `deriveKeyFromMnemonic(seed)` で MK を unwrap、
   かつ `recovery_verifier` を導出。
3. ユーザーが新パスワード入力。新 salt → 新 login material、MK を新 mk_wrap_key で再 wrap。
4. **シードローテーション (§3.4 の旧シード無効化)**: クライアントが `window.crypto.getRandomValues`
   (初回登録フローと同じ乱数源) で**新 24 語シード**を生成し、MK を新シード鍵で再 wrap、新
   `recovery_verifier'` を導出。
5. `POST /auth/recovery/finish`。フィールド命名は §3.3 (パスワード変更) との混同を避け、用途が
   自明になるよう接頭辞を付ける:
   ```
   {
     username,
     recovery_verifier,                  // 旧シード由来。サーバ認証用 (照合される)
     login_salt, login_verifier, login_kdf_params,        // 新PW由来 (login_* を更新)
     passphrase_wrapped_master_key, passphrase_wrap_iv,   // MK を新PW mk_wrap_key で wrap (passphrase wrapped_key として保存)
     recovery_wrapped_master_key, recovery_wrap_iv,       // MK を新シードで wrap (recovery_seed wrapped_key として保存)
     new_recovery_verifier               // 新シード由来。新 recovery_seed_server_hash の素
   }
   ```
6. サーバ: 旧 `recovery_verifier` を `recovery_seed_server_hash` と `hmac.compare_digest` で定数
   時間照合。**`recovery_seed_server_hash` が NULL のケース (旧ウィザード作成ユーザー / シード未設定)
   でも `hmac.compare_digest(computed, b"\x00"*32)` 等のダミー照合を常に実行してから失敗させる**
   (`if hash is None: return` の早期 return はタイミングでシード有無を漏らすので禁止)。OK なら
   **単一トランザクション**で次を更新 (= 新保存完了後に旧シード無効化、§3.4 の順序):
   - `login_*` (login_salt / login_verifier の server_hash / login_kdf_params / login_secret_version) ← 新PW由来
   - passphrase wrapped_key ← `passphrase_wrapped_master_key` / `passphrase_wrap_iv`
   - recovery_seed wrapped_key ← `recovery_wrapped_master_key` / `recovery_wrap_iv`
   - `recovery_seed_server_hash` (新) ← **`HMAC(LOGIN_SERVER_SECRET, "recovery-hash" || 0x00 ||
     new_recovery_verifier)` を計算して保存** (受信した `new_recovery_verifier` から導出。生の
     verifier は保存しない)
   - `session_token_version` インクリメント
   - `passkey_only_login = False` (passkey 紛失で復旧したユーザーが新 PW でログインできるよう
     解除。passkey_only revival、下記参照)

   passkey_prf 鍵は MK 不変でそのまま有効。version インクリメントにより当該ユーザーの既存サーバ
   セッションが全失効する (下記「セッション失効」)。
7. クライアント: **新シードを 1 回限りセキュア表示** (初回登録と同等要件)。

**セッション失効 (リセット後)**: recovery reset はパスワード変更 (§3.3) より影響が大きい (攻撃者が
シードを入手した場合の乗っ取り経路)。MK 自体は不変なので、攻撃者の既存セッションに残った解錠済み
MK (SharedWorker) や Flask-Login セッション Cookie が reset 後も生き続けるリスクがある。よって
**finish 成功時にサーバ側で当該ユーザーの全セッションを失効**させる: `User` に
`session_token_version` (INTEGER, §3.3 のパスワード変更でも将来導入候補) を持たせ、Flask-Login
の `user_loader` / `get_id` でこの version をセッションに焼き込み照合する (version 不一致 =
強制ログアウト)。reset の finish で version をインクリメントすれば、旧セッションは次回リクエストで
無効化される。新パスワードでログインし直したセッションのみが有効になる。(本機構は §3.3 にも
波及するため PR-4b で導入し §3.3 のパスワード変更にも適用する。)

**passkey_only ユーザーの扱い (脅威モデル含意)**: `passkey_only_login=True` でも recovery_seed
wrapped_key と `recovery_seed_server_hash` を持つユーザーは本フローを利用できる (「パスワード忘れ」
ではなく「パスワード認証を再有効化したい」ケース。finish で login_* が設定されるので結果的に
パスワードログインが復活する)。**含意**: ユーザーが意図的に `passkey_only_login=True` にして
パスワード認証を無効化していても、シードを持つ者が reset を実行すると passkey_only 保護が外れる。
これは**許容する** — リカバリシードを保有しているのは本人のみという前提 (シードは初回に 1 回限り
セキュア表示し、本人が物理保管する) であり、シードを持つ = 本人の最終リカバリ権限とみなす。逆に
「passkey_only を維持したまま reset したい」需要は想定しない (reset の目的がパスワード復活のため)。
リカバリシードを持たない passkey_only ユーザーは本フロー対象外 (passkey 認証で入る)。

**段階 PR 案 (PR-4b)**:
- PR-4b-1: `recovery_seed_server_hash` (BYTEA 32B) + `session_token_version` (INTEGER default 0)
  列 (マイグレ) + recovery_seed 作成時にサーバ保存 (wrapped_keys API 拡張 or 専用) + クライアント
  recovery_verifier 導出 (login_kdf.js) + Flask-Login `get_id`/`user_loader` の version 焼込み・
  照合 (§3.3 パスワード変更にも適用)。
- PR-4b-2: `/auth/recovery/begin` `/finish` エンドポイント (CSRF 免除 + レート制限) + テスト
  (レート制限・列挙耐性のダミー決定性と長さ 48B/12B 一致・旧 verifier 照合・シードローテーション・
  NULL ユーザー・finish 成功時のセッション失効)。
- PR-4b-3: 公開リセットページ UI + クライアントリセットフロー JS + 新シードセキュア表示 + E2E。
  passkey_only ユーザー向けに「この操作によりパスワード認証が有効になります」警告表示を入れる
  (上記「passkey_only revival」含意の UI 明示)。
- **既存ユーザー (`recovery_seed_server_hash` = NULL) の後埋め方針**:
  - **推奨 (primary)**: 解錠済みセッションでウィザードからシードを再入力 (または再発行) させ、
    そのとき `recovery_verifier` を計算してサーバに後付け保存する専用導線を設ける。能動的に
    seed-only リセットを有効化できる。
  - **fallback**: 上記をスキップしたユーザーは、次回シードローテ (= 何らかの理由でシード再発行)
    時に自動確立される。
  - **NULL ユーザーの begin 応答**: サーバは NULL でも `begin` を**通常どおりダミー応答で通す**
    (シード未設定と同じ列挙耐性応答)。「NULL なのでリセット不可」と即時に区別できる応答は返さない
    (列挙耐性のため)。実際のリセット可否は finish の verifier 照合で判定 (NULL ユーザーは必ず失敗)。
    移行期に seed-only リセットが使えないユーザーは passkey / 再ログイン経由になる旨を UI に明記。

**実装チェックリスト (PR-4b-1/2 に転記)**:
- **`session_token_version` の後方互換 (PR-4b-1)**: マイグレで `session_token_version INTEGER
  NOT NULL DEFAULT 0` を設定。既存 Flask-Login セッション Cookie には version 情報が無いため、
  `user_loader` は **Cookie に version が無い旧形式 = version 0 として扱い**、`User.session_token_version`
  が 0 のうちは透過的に通過させる (= 既存ログインセッションを切らない)。`get_id()` は
  `f"{id}.{version}"`、`user_loader` は `.` で分割し version 不一致なら拒否 (version 欠如時は 0)。
  テストに「version 無し Cookie が version=0 ユーザーで通過」「DB の version を 1 に上げると旧
  Cookie が拒否される」の後方互換ケースを含める。
- **CSRF 免除の確認 (PR-4b-2)**: `recovery/begin|finish` を `csrf.exempt` する前に、`auth` BP の
  `login/begin|finish` が実際に exempt されていることをコードで確認する (CLAUDE.md の exempt 一覧は
  現状 WebAuthn / REST API のみ記載)。確認後、CLAUDE.md の `csrf.exempt` 対象一覧に
  `recovery/begin|finish` を追記する。
- **レート制限の粒度 (PR-4b-2)**: login API と同等以上に厳格化する。`begin`: per-IP `5/minute` +
  per-username `10/hour`。`finish`: per-IP `5/minute` + per-username `5/hour` (verifier 総当たり
  抑止)。limiter の teardown reset (フルスイート leak 対策) もテストに入れる。
- **finish の NULL ハッシュ定数時間照合 (PR-4b-2)**: `recovery_seed_server_hash` が NULL でも
  `hmac.compare_digest(computed, b"\x00"*32)` のダミー照合を常に実行してから失敗させる
  (None チェックで早期 return 禁止)。テストに「NULL ハッシュでも `compare_digest` が 1 回呼ばれ、
  かつ必ず失敗する」ケースを含める。
- **CLAUDE.md の CSRF 免除一覧更新 (PR-4b-2)**: 現状の記載 (WebAuthn / REST API のみ) は実態と
  乖離 (`auth_api_bp` / `wrapped_keys_bp` / `keypair_bp` / `audit_packages_bp` / `ai_config_api_bp`
  / device authorization / oauth token も免除済)。recovery bp 追加時に一覧を全面更新する。

### 3.5 自然な v4 → ログイン派生移行 (in-login・本方式の中核)

既存 v4 ユーザー (werkzeug `password_hash` 有 / `login_salt` 無) が**初回ログイン時に
透過移行**する。前提として v5 デプロイ時に `e2ee_data_migration.py` が全平文をサーバ側
**temp-MK** で暗号化済み (`users.migration_temp_mk` に保持) で、平文列は物理 DROP 済。よって
本フローが行うのは**認証因子の移行**と **temp-MK→自分の MK への rewrap 駆動**であり、生平文の
暗号化ではない。

> ⚠️ **temp-MK 平文 DB 保存リスク**: `migration_temp_mk` は移行窓中 DB に平文保持される既知
> リスク。`index.md §13.9` の警告 (KMS/HSM 暗号化 ToDo・移行窓を最短化) を参照。本フローは
> ⑤ finalize で**ユーザー単位に即時** temp-MK を破棄することでこの露出窓を最小化する。

```
[ログイン画面] パスワード 1 回入力
  │
  ├ ① POST /auth/login/begin {username}
  │     → サーバ: password_hash 有 / login_salt 無 を検出
  │     → 新規 salt(16B) を生成し Flask session に一時保存 (pending_login_salt)
  │     → {salt: 新規 16B, kdf_params, migration_required: true}
  │
  ├ ② クライアント: master = Argon2id(password, salt)
  │     login_verifier = HKDF(master,"iikanji-login-v1")
  │     mk_wrap_key   = HKDF(master,"iikanji-mk-wrap-v1")
  │     MK = random(32B)              ← 本人専用の新 MK を生成
  │     {wrapped_master_key, wrap_iv} = AES-256-GCM(mk_wrap_key, MK)
  │
  ├ ③ POST /auth/login/finish (移行パス)
  │     {username, password, login_verifier, login_salt, login_kdf_params,
  │      wrapped_master_key, wrap_iv}
  │     → サーバ: (a) user.check_password(password) で werkzeug を最終 1 回検証
  │              失敗 → 汎用エラー・移行しない (⚠️ §2 例外。下記「平文…」参照)
  │            (a') 送られた login_salt が session の pending_login_salt と
  │                 一致するか確認 (compare_digest)。不一致 → 拒否
  │            (b) 単一トランザクションで login_server_hash / login_salt /
  │                login_kdf_params / login_secret_version を設定し、
  │                wrapped_keys(method='passphrase') を UPSERT
  │            (c) ★ password_hash はまだ残す (rewrap 未完のため)
  │            (d) commit → セッション確立 → migration_temp_mk 有無を返す
  │
  ├ ④ クライアント: MK を SharedWorker へ。migration_temp_mk 有なら:
  │     GET /api/v1/migration/temp-mk → temp-MK 取得
  │     [進捗バー] 6 テーブル (je/jel/me/vmeta/valog/bcb) を
  │       POST /api/v1/migration/rewrap で temp-MK→自 MK に再暗号
  │       (証憑画像は PUT /api/v1/migration/rewrap-image)
  │
  └ ⑤ POST /api/v1/migration/finalize
        → サーバ: migration_temp_mk を NULL クリア (UPDATE users SET
                migration_temp_mk=NULL。index.md §13.9 と整合)
                + ★ password_hash を NULL クリア + 移行完了マーク
```

**⚠️ 平文パスワードの一時送信 (§2「平文をサーバに送らない」原則の例外)**:
③ の移行パスでは `password` (平文) をサーバへ送る。werkzeug の `check_password_hash()` が
平文を必要とするためで、**移行フロー (`migration_required=true`) に限定した一時的例外**。
通常ログイン (§3.2) は `login_verifier` のみで §2 原則を維持する。受動管理者モデル (§4-2) が
「DB とログを読むだけ」を攻撃者像とする以上、平文がログに残らないことが必須。**PR-2 実装
チェックリスト** (漏洩経路は Sentry だけではない — アクセスログ・APM・デバッグ出力の**すべて**で
抑制する):
- **アクセスログ**: gunicorn `--access-logformat` にボディを含めない。Flask `after_request` で
  リクエストボディをログしない。
- **APM / エラー監視**: Sentry `before_send` / Datadog 等で `password` フィールドを明示除外。
  Flask デバッグモードは本番無効 (スタックトレースにローカル変数が出る)。
- **dict からの除去**: Python の `del password` は**ローカル変数の参照を切るだけ**で、
  `request.get_json()` が返す dict には `password` キーが残る。`data = request.get_json();
  password = data.pop("password")` のように **dict からも pop** してから後続処理する。
  `password` の参照は `check_password()` 引数渡しのみとし直後にスコープから外す。
- この平文送信は v5 デプロイ後の各 v4 ユーザー初回ログイン 1 回限りで、移行完了後は二度と
  発生しない (finalize で `password_hash` クリア → 以後 `login_verifier` のみ)。

**一時 salt の管理 (改ざん・競合防止)**: ① でサーバが発行する移行用 salt は **Flask session に
`pending_login_salt` として一時保存**し、③ で送られた `login_salt` と `compare_digest` で
一致確認する (③ a')。これによりクライアントのバグや実装ミスで任意 salt が送られても弾ける
(TLS 前提でも多層防御)。中断後 ① を呼び直すと新 salt で上書きされ、最後に呼んだ ① の salt で
③ が成立する (前回 salt との競合は session 上書きで解消)。③ 成立後は `pending_login_salt` を
session から削除する。
- **マルチインスタンス注記**: `pending_login_salt` を Flask session に置く以上、`/begin` と
  `/finish` が**別インスタンスに到達すると salt が引けず移行が失敗する**。現行本番 (Tailscale +
  単一コンテナ) では問題ないが、**水平スケール時は共有 session ストア (`SESSION_TYPE=redis`
  等) か sticky session が前提**になる。スケール前にこの前提を満たすこと。

**移行の完了条件**: ③ で認証因子は移行されるが、移行が「完了」するのは ⑤ finalize 後。
`password_hash` は finalize まで残し、temp-MK 破棄と同時にクリアする。

**中断耐性 (冪等・再開可能)**:
- ③ commit 前にクラッシュ → `login_salt` 未設定のまま。次回ログインも `migration_required: true`
  で**やり直し** (`password_hash` 無傷)。
- ③ commit 後・⑤ 前にクラッシュ (rewrap 途中) → `login_salt` は設定済なので次回 begin は
  **通常パス** (`migration_required: false`)。`login_verifier` で認証後、`migration_temp_mk`
  がまだ有ることを検出して **rewrap を resume** する。`/migration/rewrap` は処理済み行を skip
  する idempotent 実装なので二重暗号化は起きない。resume 完了で ⑤ finalize。
- したがって**いつ中断しても安全**で、データが二重暗号化・破損する経路は無い。
- **passkey_only / パスワード未設定ケース** (§3.2 の `requires_password_setup`): パスワード
  未設定のまま ③ finish に到達した場合 (`login_verifier`/`wrapped_master_key` をクライアントが
  生成できない) は**移行を成立させない** (汎用エラーで reject)。パスワード設定 UI を完了して
  はじめて ②③ に進める。両列 NULL の異常状態も同じく通常認証へは入れず、強制パスワード設定に
  誘導する。
- **`pending_login_salt` の孤立 (別端末 race)**: ① で `requires_password_setup` を受けた端末 A が
  パスワード設定 UI 操作中に、別端末 B が passkey 認証で別セッションを確立しても、A の
  `pending_login_salt` は A の session cookie 内に閉じており B には影響しない。A が後から ③ を
  投げても、(i) session の salt 一致確認、(ii) `login_salt IS NULL` (未移行) 判定、(iii) werkzeug
  検証の三段で守られる。途中で別経路により移行が完了 (`login_salt` セット) していれば ③ は
  **`migration not applicable` で reject** され、孤立 salt は session 失効で自然消滅する
  (二重移行・不整合は起きない)。

**gate との関係**: 本フローはログイン中 (セッション確立前後) に駆動するため、E7 の鍵未設定
gate (`is_active=False` → `/migration/locked`) を**移行誘導には使わない**。gate は「30 日 stale
ロック」「退会導線」の役割としてのみ残す (§7.2)。

### 3.6 TOTP 2 要素認証 (opt-in) + `passkey_only_login` 廃止

`passkey_only_login` を廃止し全ユーザーにパスワード (= 常用鍵) を必須化する (§7.2) と、
Passkey 未設定ユーザーの常用ログインが**パスワード単一要素**になる。これを補うため TOTP
(RFC 6238) を第 2 要素として追加する。

**確定した設計判断 (ユーザー決定 2026-06-07)**:
1. **適用範囲 = 任意 (opt-in)・推奨**。設定画面で各ユーザーが有効化、既定オフ。家計簿アプリで
   デバイス紛失ロックアウトを避けつつ希望者は 2FA 可。強く推奨表示する。
2. **2FA 充足 = Passkey or TOTP**。Passkey は既にフィッシング耐性ある強因子なので、Passkey
   保有者は TOTP を**設定しなくてよい** (Passkey ログインで所持因子を満たせる)。UI で「Passkey
   設定済みなら TOTP は任意」と案内する。実装は単純に `totp_required = user.totp_enabled`
   (TOTP を有効化した人だけパスワードログインで TOTP を要求)。
3. **リカバリシードでのリセットは TOTP をバイパス** (seed = 全権復旧因子、§3.4.1)。

#### 3.6.0 この E2EE モデルでの TOTP の守備範囲 (正直な明記)

TOTP は `finish` の**サーバ側ゲート**であり、守れるもの/守れないものが普通の Web と異なる:

| シナリオ | TOTP の効果 |
|---|---|
| **(a) パスワード単独漏れの遠隔攻撃** (フィッシング/使い回し/他所漏洩) | ✅ ブロック (デバイス無しでは finish を通せない) = TOTP 最大の価値 |
| (b) サーバ DB 丸ごと漏洩 + パスワード既知 | ❌ 無力 (攻撃者は `wrapped_master_key`/blob を DB 直取り → パスワードでオフライン復号。防御は Argon2id コスト + `login_server_hash` が HMAC) |
| (c) リアルタイム中継フィッシング (TOTP コード即転送) | △ 弱い (TOTP はフィッシング耐性なし。Passkey の領分) |

- **TOTP secret は MK 派生に混ぜない**。揮発的 (デバイス紛失 = 全データ喪失) になるため、
  あくまでアクセスゲート専用。
- **TOTP secret はサーバが検証時に復号する必要があるので E2EE 不可** (`wrapped_keys`/MK 方式は
  使えない)。サーバ鍵で at-rest 暗号化する (§3.6.1)。これは「サーバが読める認証用シークレット」
  でありユーザーデータではないので E2EE 原則と矛盾しない。

#### 3.6.1 TOTP secret のサーバ側 at-rest 暗号化

- 暗号鍵: `totp_enc_key = HKDF-SHA256(LOGIN_SERVER_SECRET, salt=zero(32),
  info="iikanji-totp-enc-v1", L=32)`。`LOGIN_SERVER_SECRET` から専用 info で導出し、
  login_verifier / recovery のドメインと分離する。`salt=zero(32)` は**意図的** —
  `LOGIN_SERVER_SECRET` 自体が高エントロピー鍵素材なので可変 salt は不要 (RFC 5869 §2.2、
  §2 の他の HKDF と同方針)。
- 保存: TOTP secret (RFC 4226 推奨 20B = 160bit) を `AES-256-GCM(totp_enc_key, iv,
  plaintext=secret, aad=str(user_id))` で暗号化し、`users.totp_secret_encrypted`
  (BYTEA = ciphertext 20B + GCM tag 16B = **36B**) / `users.totp_secret_iv` (BYTEA 12B) に格納。
  **AAD に user_id を含める**ことで別ユーザーへの暗号文移植を防ぐ。
- **`secret_version` 共有**: `LOGIN_SERVER_SECRET` をローテすると `totp_enc_key` も変わるので
  再暗号化が要る。`login_secret_version` を TOTP にも適用し、verify 成功時 (= secret を復号
  できた瞬間) に新鍵で再暗号化する遅延ローテ (§3.1 と同方針)。
- 実装: `cryptography>=48` の `AESGCM` を利用 (既に `migration_crypto.py` で使用)。
  `login_derived.py` に `totp-enc` の HKDF ラベルと暗号化/復号ヘルパーを追加する。

#### 3.6.2 登録フロー (opt-in・verify-before-enable)

1. 設定画面で「TOTP を有効化」。サーバが secret (20B) を生成 → at-rest 暗号化して保存
   (`totp_enabled=False`)。`otpauth://totp/...` URI + QR を応答に乗せる。**secret 平文は登録時の
   1 回だけ応答に乗る** (QR 表示に必須、TLS 前提)。DB には暗号文のみ。
2. ユーザーが authenticator アプリに登録 → 6 桁コードを入力して**確認**。サーバが secret を
   復号し `pyotp` で検証 → 成功で `totp_enabled=True` + `totp_confirmed_at` セット。
3. **verify-before-enable**: 確認コードが通るまで `totp_enabled=False` のまま (誤登録による
   ロックアウト防止)。確認成功時に**バックアップコードを発行・表示** (§3.6.3)。

**ライフサイクル (disable → re-enable)**:
- **無効化時** (`totp_enabled=True → False`): `totp_secret_encrypted` / `totp_secret_iv` /
  `totp_confirmed_at` / `totp_last_used_step` を **NULL クリア**し、バックアップコードを全件
  無効化 (物理削除 or 全 `used_at` セット)。旧 secret は再利用しない。
- **再有効化時**: 必ず**新しい secret を生成**する (§3.6.2 step1 から再実行)。旧 authenticator
  登録を消したユーザーが新 QR で確実に再登録できる。
- **未確認登録中の競合**: `totp_enabled=False` で確認待ちの間に再度「有効化」を叩くと secret が
  上書きされうる。確認前 secret はまだ誰も使えないので**最後の生成で単純上書き**してよい
  (確認は最新 secret に対してのみ成立)。確認 (`enable`) は `totp_enabled=False` を条件に含めた
  単一 UPDATE で行い、二重確認をはじく。

#### 3.6.3 バックアップコード

- **フォーマット (確定)**: 1 コード = `secrets.token_hex(5)` = **10 桁 hex** (40bit)。表示は
  `xxxxx-xxxxx` のようにグループ化。**N=10 個**発行。`recovery_code` のハッシュ方式に揃え
  **SHA-256 hexdigest を保存** (平文は発行時 1 回だけ表示)。`recovery_code` との差分は「単一
  コードでなく 10 個のセット」「typeable のため hex 40bit (recovery_code は token_hex(32)=256bit)」。
  40bit でも 1 回限り使用 + finish のレート制限 + 10 個上限で総当たりは非現実的。
- **保存テーブル `totp_backup_codes`**: `user_id` / `code_hash` (CHAR 64, SHA-256) /
  `code_prefix` (表示用 先頭 4 桁 + "...") / `used_at` (TIMESTAMPTZ nullable)。1 回限り使用
  (`used_at` でマーク)。
- **再生成**: 旧コードを**全件物理削除**してから新 10 個を発行する (旧コードは即無効)。
- TOTP デバイス紛失時の入口。**最終手段はリカバリシード** (§3.4.1。reset は TOTP をバイパス)。

#### 3.6.4 ログイン統合 (2 ラウンドへの注入)

- **`/auth/login/begin`**: 応答に `totp_required: bool` を追加。`user 実在 && totp_enabled` で
  true、それ以外 (未知ユーザー含む) は false。決定的なので列挙耐性は §3.2 の `migration_required`
  と同レベル (ユーザー名既知前提では「TOTP 有効か」が既存ユーザーについて漏れるが許容)。
- **`/auth/login/finish`**: `totp_required` のユーザーは `totp_code` と
  **`totp_type` ("totp" | "backup")** を送る。**判別はクライアントが明示する `totp_type` で行う**
  (6 桁数字の正規表現推測に頼らない — バックアップコードが偶然 6 桁になる曖昧さを排除)。サーバは
  **login_verifier 照合 OK の後に**:
  - `totp_type=="totp"`: secret を復号し `pyotp.TOTP.verify` (時刻ずれ ±1 step 許容)。
  - `totp_type=="backup"`: `totp_backup_codes` を SHA-256 で照合し、未使用なら `used_at` セット。
  - 失敗なら 401 (login_verifier が正しくても拒否)。
- **replay 対策 (必須・PR-T3 で実装)**: TOTP 成功時に使用した step (= `floor(unixtime/30)`) を
  `users.totp_last_used_step` に記録し、**同一 step の再利用を拒否**する (RFC 6238 推奨。30 秒窓内の
  盗聴即転送を防ぐ)。
- レート制限: TOTP の総当たり (10^6) 抑止のため `finish` の per-username 制限を強化する。具体値:
  **per-username `5/minute` + `20/hour`、連続失敗 5 回で当該ユーザーを 15 分一時ロック** (失敗
  カウンタは成功でリセット)。バックアップコードにも同レート制限を適用。
- **CSRF**: TOTP 登録/管理 API (設定画面、ログイン済み) は **CSRF 保護を維持** (通常の Web フォーム
  /htmx 経由)。ログイン経路の `/auth/login/finish` は既存どおり JSON 専用で **CSRF 免除**
  (auth_api の方針)。

#### 3.6.5 リセット (§3.4.1) / パスワード変更 (§3.3) との関係

- **リカバリシードリセットは TOTP をバイパス** (seed = 全権)。`/auth/recovery/finish` で
  `totp_enabled=False` に初期化 + `totp_secret_encrypted`/`totp_secret_iv` クリア +
  バックアップコード無効化する (デバイスも紛失している可能性が高いのでリセット後に再設定させる)。
  → §3.4.1 finish の更新項目に追補 (実装は TOTP 導入 PR で §3.4.1 の finish に足す)。
- **パスワード変更 (§3.3) は TOTP に影響しない** (MK 不変、TOTP secret は別管理で据え置き)。

#### 3.6.6 `passkey_only_login` 廃止

- §7.2 の方針を実装に落とす。全ユーザーにパスワード必須化。`passkey_only_login` 分岐を撤去:
  `auth.py` のパスワードログイン弾き、`settings.py` の `passkey_only_enable/disable`、関連
  テンプレート (`passkeys.html` / `delete_account.html`)、`forms/settings.py`。
- 既存 passkey_only ユーザー (`password_hash` NULL の可能性) は §3.5 移行時 or 設定でパスワード
  設定を促す。リカバリシードリセットでも `passkey_only_login=False` に解除済み (§3.4.1 / PR-4b-2)。
- 列は後続マイグレで物理 DROP (または常時 False に固定し UI 撤去)。
- **段階的実装 (採用、2026-06-07)**: **PR-T4** で振る舞いを除去 (enable/disable ルート撤去 +
  保護ロジックを `passkey_only_login` → **`password_hash` 有無** に付け替え + auth.py の
  パスワードログイン弾き撤去 + UI 撤去) するが、**列・モデル属性は据え置き (DROP しない=可逆)**。
  保護の実条件は「再認証用パスワードの有無」なので `password_hash` で判定する (TOTP begin/confirm
  ガード・最後のパスキー削除ブロック・退会の再認証要否)。**列の物理 DROP は population ゼロ
  (オーナーがパスワード経路へ移行) 確認後の後続 PR (PR-T4-drop)**。`recovery_finish` の
  `passkey_only_login=False` は無害なため据え置き、DROP 時に除去する。
- **`password_hash=NULL` かつ `login_salt=NULL` ユーザーの fallback (詰み防止)**: パスワードを
  持たないユーザーは `/auth/login/finish` がそもそも成立しない (`login_salt` 無しで照合不能 →
  通常パス 401)。よって PR-T4 で `passkey_only_login` 強制を撤去しても**勝手にログイン不能には
  ならない** (元々パスワードで入れない)。このユーザーの入口は次の 3 つに限定される:
  1. **Passkey ログイン** (WebAuthn 経路。従来どおり有効)
  2. **リカバリシードによるリセット** (§3.4.1。新パスワードを設定して password 経路を開通)
  3. ログイン画面で「パスワード未設定」を検知したら**パスワード設定フローへ誘導**
     (`/auth/login/begin` は password_hash 無 & login_salt 無のユーザーに既に
     `requires_password_setup: true` を返す実装がある → これを UI で受けて設定させる)
  PR-T4 はこの 3 経路が機能することを E2E で確認してから `passkey_only_login` 列を DROP する。

#### 3.6.7 段階 PR 案 (TOTP)

- **PR-T1**: secret 保管基盤 — マイグレ (`users.totp_secret_encrypted`/`totp_secret_iv`/
  `totp_enabled`/`totp_confirmed_at`/`totp_last_used_step` + `totp_backup_codes` テーブル) +
  `login_derived` に `totp-enc` ラベルと AES-GCM 暗号化/復号ヘルパー + `pyotp` 依存追加 + テスト。
  **既マージの `/auth/recovery/finish` (#403) にパッチ**を当て、リセット成功時に totp_* 列を
  クリア + バックアップコードを無効化する (§3.6.5。実装者が見落とさないようスコープに明記)。
- **PR-T2**: 登録/確認/バックアップコード発行 UI + API (settings)。verify-before-enable。
- **PR-T3**: ログイン統合 (`begin` の `totp_required` + `finish` の `totp_code` 検証 +
  バックアップコード受理 + レート制限) + 実ブラウザ E2E。
- **PR-T4**: `passkey_only_login` 廃止 (コード撤去 + 列 DROP マイグレ + 既存ユーザー移行導線)。
- recovery finish の TOTP 初期化 (§3.6.5) は PR-T1 or PR-T3 で §3.4.1 finish に追補。

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
   - **TOTP のフィッシング非耐性 (§3.6)**: TOTP (opt-in 第 2 要素) はパスワード単独漏れの
     遠隔攻撃を塞ぐが、リアルタイム中継フィッシングには弱い。フィッシング耐性が要るユーザーには
     Passkey を推奨する (TOTP は普遍的に使える代替)。サーバ DB 漏洩 + パスワード既知の
     オフライン復号は TOTP では防げない (1 と同根)。
4. **移行窓中の状態漏洩 (限定的)**: `login/begin` の `migration_required: true` は対象が
   「v4 移行未完了」であることを示す。未知ユーザーへは常に `false` を返すため新規列挙は防げる
   が、**既存ユーザーに対しては移行状態が伝わる**。許容リスク (移行窓中限定・移行完了で
   `migration_required` は恒久的に `false` になり消滅) として受容する。

## 5. OPAQUE 採用可否

OPAQUE (aPAKE) を使えば「サーバは salt 相当も見ない + 列挙耐性」まで厳密化できる
が、(a) §4-1 の MK オフライン攻撃は OPAQUE でも消えない、(b) 実装が複雑で vetted
ライブラリ依存、という理由で **第一段では HKDF split を採用**。将来、認証強化が
必要になれば OPAQUE への置換を検討 (login_verifier 層だけ差し替え可能な設計に
しておく)。

## 6. 段階 PR 案

0. **PR-0 設計書リワーク (本 PR)**: §7 の「クリーンカットオーバー」を **§3.5 自然移行** に
   差し替え、§3.2 `migration_required` / §7.1 段階 nullable→drop / §7.2 gate 整理 / #386
   レビュー申し送り (HKDF info byte エンコード・`hmac.compare_digest`・鍵破棄・旧シード無効化
   タイミング) を反映。コードなし。
1. **PR-1 クライアント KDF プリミティブ**: `master/login_verifier/mk_wrap_key` 派生
   (argon2.js + HKDF) と単体テスト (golden vector)。
2. **PR-2 ログイン 2 ラウンド API + 透過移行パス**: `/auth/login/begin` `/finish` + サーバ側
   `server_hash` 保管。列挙耐性 (ダミー salt)。**§3.5 の移行 finish パス** (werkzeug 最終検証 +
   login material 確立) を含む。**`/begin` / `/finish` を新規追加し、旧 `/login` は PR-3 で
   廃止予定として残す** (PR-2 単独では破壊せず共存。下記「過渡期」のため)。マイグレ `070` で
   `login_*` 列追加 + `password_hash` nullable 化 (drop は後続)。
   - **レート制限**: `/auth/login/begin` は**認証前**に呼ばれ列挙の攻撃面になるため、
     既存ログイン系と同等の `@limiter.limit("5-10/minute")` を**必ず付与**する
     (ダミー salt でも応答時間差での存在判定を避けるため定数時間で応答)。`/finish`
     も同様にレート制限。
   - **定数時間応答の実装**: `/begin` は既知/未知ユーザーで応答時間差を作らない。既存
     `recovery_login` の `_dummy_check()` (`compare_digest` でダミー検証) と同等の方式を採り、
     未知ユーザーでもダミー salt 計算 (HMAC) を実行して早期 return しない。
   - **temp-MK 系エンドポイントの棲み分け**: `§3.5 ④` の `temp-mk` / `rewrap` / `finalize` は
     既存実装 (`app/views/api.py`、`/api/v1` Blueprint・CSRF 免除・Bearer/セッション両対応)
     を**そのまま再利用**する (移行専用に新 prefix を切らない。既存 E7 機構との二重化回避)。
   - **PR-3 までの過渡期**: PR-2 が先行マージされる間、`passkey_only_login` 制御は
     既存 `auth.py` の `/login` が引き続き担保する (新 `/begin`/`/finish` には §7.2 の
     パスワード必須化が PR-3 で入るまで passkey_only ユーザーを通さないガードを置く)。
3. **PR-3 ウィザード再構成 + 透過 rewrap ドライバ**: 「パスワード = 鍵」前提に。初回登録で
   パスワードから MK 確立 + リカバリシードを**必須バックアップ**として提示。passphrase 単独方式は
   廃止 (= login password に統合)。Passkey/リカバリは追加・緊急として残す
   (鍵の追加・削除 UI は実装済)。**§3.5 ④ の temp-MK→自 MK rewrap 進捗ドライバ**をログイン後
   フローに組込み、gate+3 方式ウィザードの移行誘導を廃止する。**データ暗号化パスは E7 で完了済
   なので本 PR では不要** (透過 rewrap のみ)。**この PR で `index.md §2 / §10` の鍵管理記述を
   本方式に更新する** (`passphrase` method の意味変化を反映)。
4. **PR-4 パスワード変更/リセット**: MK 不変・ラップ更新。リカバリ経由リセット。
5. **PR-5 client-py / TUI**: 同じ派生を実装 (web と byte 互換)。
6. **ドキュメント**: 設計書 §1 (能動脅威の限界と native client 信頼根拠) / §2 / §10
   を本方式に更新。

## 7. 影響範囲・DB スキーマ差分

### 7.1 DB スキーマ
- `users` に `login_server_hash` (= `HMAC-SHA256(LOGIN_SERVER_SECRET, login_verifier)`、
  **BYTEA 32B**) と `login_salt` (**BYTEA 16B**) + `login_kdf_params` (JSON) +
  `login_secret_version` (SMALLINT、§3.1 の遅延ローテーション用) を追加する。
  - **列型は BYTEA** (base64/hex の VARCHAR ではない) で確定する。`login_server_hash` は HMAC
    生 32B、`login_salt` は乱数生 16B をそのまま格納する (実装間の byte 互換と無駄なエンコード
    往復回避のため)。`login_kdf_params` は `{memory, iterations, parallelism}` の JSON。
  - **`password_hash` は段階的に撤去する** (自然移行のため即 drop しない):
    1. マイグレ `070` で `login_*` 列追加と同時に `password_hash` を **nullable=True** 化。
    2. §3.5 の初回移行 finish で werkzeug 検証 → finalize 時に該当ユーザーの `password_hash` を
       NULL クリア (移行済みフラグも兼ねる)。
    3. **全ユーザーの移行完了後** (移行窓終了後)、**後続マイグレで `password_hash` 列を物理 DROP**
       + werkzeug 依存を撤去する。`login_salt IS NOT NULL` を移行済み判定に使う。
- `wrapped_keys` テーブルの `method` enum は**変更しない** (`passkey_prf` /
  `passphrase` / `recovery_seed`)。`login` という新 method は**作らない** ——
  「login パスワード由来の鍵」は従来 `passphrase` method の wrapped_key として
  保存する (派生元がログインパスワードに変わるだけで、wrap の仕組みは同じ)。
  これにより鍵の追加・削除/解錠 UI を再利用できる。
  **注**: `index.md §2 / §10` は現状「パスフレーズ (フォールバック)」= 別途設定する
  passphrase として記述しており、本方式での意味変化 (= ログインパスワード由来) は
  **PR-3 で `index.md` を更新して反映**する (それまでは本設計書が正)。
  > ⚠️ **実装者注**: `wrapped_keys.method='passphrase'` の **CHECK 制約値自体は不変**だが、
  > その wrapped_key の「元になる認証情報」が本方式 (PR-3 以降) では**ログインパスワード**に
  > 変わる (鍵派生ロジックの変更)。`index.md §10.1` を読んで従来の「別途設定したパスフレーズ」
  > として実装しないこと。
- 既存ユーザーはクリーン再作成しない。§3.5 の in-login 透過移行でアカウント・データを保持する。
- **TOTP 2FA 用の列 (§3.6)**: `users` に `totp_secret_encrypted` (BYTEA 36B = 暗号文 20B + tag
  16B) / `totp_secret_iv` (BYTEA 12B) / `totp_enabled` (BOOLEAN default false) /
  `totp_confirmed_at` (TIMESTAMPTZ nullable) / `totp_last_used_step` (BIGINT nullable、replay 対策
  §3.6.4) を追加。バックアップコードは別テーブル `totp_backup_codes`
  (`user_id`, `code_hash` CHAR64 SHA-256, `code_prefix`, `used_at`) で 1 回限り使用を管理。
  TOTP secret は MK でなく `LOGIN_SERVER_SECRET` 由来鍵で at-rest 暗号化する (§3.6.1)。

### 7.1.1 移行ゲート (`is_active=False` / `/migration/locked`) の役割整理
- E7 の鍵未設定ゲート (`migration_lock_gate` @ `app/__init__.py`、`/migration/locked`) は
  **本方式の移行誘導には使わない**。§3.5 の透過移行はログインフロー内で完結するため、ゲートで
  別ウィザードへ誘導する必要がない。**gate + 3 方式ウィザードによる移行導線は廃止**する。
- ゲート自体は残し、役割を「30 日 stale ロック (PR-4b-2 の `migration-lock-stale`)」と
  「退会導線」に限定する。3 方式選択 UI (`encryptionKeyWizard` の `choose` ステップ) は
  passphrase 単独選択を外す形で PR-3 で再構成する。

### 7.2 `passkey_only_login` ユーザーの扱い
- 現行 `auth.py` はパスワードログインを passkey_only ユーザーに対し弾く
  (`app/views/auth.py`)。本方式は「パスワード = 常用鍵」が前提なので衝突する。
- **方針 (確定)**: v5.x では **`passkey_only_login` を廃止**し、**全ユーザーに
  パスワード (= 常用鍵) を必須**とする。Passkey は §1 のとおり**任意の上乗せ**
  (Passkey PRF → `mk_wrap_key` 相当を別 wrapped_key として追加できる) に限定。
  - 理由: パスワード非保有だと「ログイン = MK 解錠」の単一入力 UX が成立せず、
    Passkey PRF の環境依存 (Bitwarden 非対応等) で常用解錠が不安定になるため。
  - v5.0 時点の passkey_only ユーザーが居れば、§3.5 の初回移行時にパスワード設定を必須化
    する (アカウントは保持。パスワード未設定だと移行 finish が成立しないため、移行 UI で
    パスワード設定を促す)。
- **パスワード単一要素の補強は TOTP (§3.6)**: passkey_only 廃止でパスワード単一要素になる
  ユーザー向けに、opt-in の TOTP 2FA を提供する。2FA 充足 = Passkey or TOTP。`passkey_only_login`
  の具体的な撤去手順・既存ユーザー移行・列 DROP は §3.6.6 を参照。

### 7.3 その他
- `auth.py` (login 2 ラウンド化 / register / password 変更 / recovery)。
- API 認証 (Bearer トークン) は別系統で影響限定。
