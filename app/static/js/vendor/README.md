# Vendored Third-Party JS

E2EE 鍵管理基盤で使う暗号ライブラリを **同梱配信** する。CDN を介さないことで
SRI 検証なしの動的 import (`<script integrity>` 非対応) によるサプライチェーン
攻撃リスクを排除する。

## hash-wasm

- **ファイル**: `hash-wasm-4.12.0.esm.min.js`
- **ライセンス**: MIT (`LICENSE-hash-wasm`)
- **元 URL**: https://cdn.jsdelivr.net/npm/hash-wasm@4.12.0/dist/index.esm.min.js
- **SHA-384**: `dcc63592b3af5d4eca28b807cd04955536c372d1bbe1870b98e3dd07eceb8ad2fad41f63a7c7e4bb3cc2119d6e472434`
- **用途**: Argon2id によるパスフレーズ → derived_key 派生 (E1 PR-F1)
- **更新手順**:
  1. 公式リポジトリ (https://github.com/Daninet/hash-wasm) で新バージョン確認
  2. `curl -sL https://cdn.jsdelivr.net/npm/hash-wasm@<VER>/dist/index.esm.min.js -o app/static/js/vendor/hash-wasm-<VER>.esm.min.js`
  3. `shasum -a 384 -b <ファイル>` で SHA-384 を取得し本 README に記録
  4. `hash_wasm_loader.js` の `HASH_WASM_VERSION` を更新
  5. 古いバージョンファイルは削除
  6. 動作確認 (鍵設定ウィザードでパスフレーズ登録)

## hpke (HPKE / RFC 9180)

- **ファイル**: `hpke-1.8.0.esm.min.js`
- **ライセンス**: MIT (`LICENSE-hpke`)
- **構成**: `@hpke/core@1.9.0` + `@hpke/dhkem-x25519@1.8.0` (hpke-js 1.8.0 リリースライン)
  を esbuild で 1 ファイルにバンドル。base mode のみ (DHKEM-X25519-HKDF-SHA256 /
  HKDF-SHA256 / AES-256-GCM)。chacha20poly1305 / x448 / P-256 等は含めない。
- **元リポジトリ**: https://github.com/dajiaji/hpke-js
- **SHA-384**: `y9qe7i9M/bidhW34EtvLTtDIqyIIuwQKHcWN5c2T0Wk/Tkjv4evdKww2OInMn1LI`
- **用途**: E5 #112 監査連携の HPKE seal/open (`crypto/hpke_suite.js`)。owner→auditor の
  スナップショット暗号化 / auditor→owner の修正案暗号化。将来 client-py の `hpke-py` と
  RFC 9180 base mode で相互接続する。
- **更新手順** (バンドル再生成):
  1. 一時ディレクトリで `npm install hpke-js@<VER>`
  2. entry: `export { CipherSuite, HkdfSha256, Aes256Gcm } from "@hpke/core";`
     `export { DhkemX25519HkdfSha256 } from "@hpke/dhkem-x25519";`
  3. `npx esbuild entry.mjs --bundle --format=esm --minify --target=es2022 --banner:js='/*! HPKE ... */' --outfile=hpke-<VER>.esm.min.js`
  4. `openssl dgst -sha384 -binary <ファイル> | openssl base64 -A` で SHA-384 を取得し本 README に記録
  5. `crypto/hpke_suite.js` の import パス (バージョン) を更新、古いファイルは削除
  6. 動作確認: `node --test tests/static/js/test_audit_hpke.mjs` (seal→open ラウンドトリップ)

## 監査

vendor ファイル変更は `gh pr review` 経由で都度確認可能 (git diff)。
バイナリ的に大きいため (~210 KB)、PR レビューでは:

1. diff の最初の行 `/*! hash-wasm (https://...) ... */` で公式リポジトリ由来であることを確認
2. SHA-384 が一致するか手元で再計算 (`shasum -a 384 -b`)
3. 更新理由 (CVE 修正 / 機能追加) を PR 本文に記載
