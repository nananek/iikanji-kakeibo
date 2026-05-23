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

## 監査

vendor ファイル変更は `gh pr review` 経由で都度確認可能 (git diff)。
バイナリ的に大きいため (~210 KB)、PR レビューでは:

1. diff の最初の行 `/*! hash-wasm (https://...) ... */` で公式リポジトリ由来であることを確認
2. SHA-384 が一致するか手元で再計算 (`shasum -a 384 -b`)
3. 更新理由 (CVE 修正 / 機能追加) を PR 本文に記載
