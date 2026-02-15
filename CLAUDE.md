# いいかんじ™家計簿 - Claude Code ガイド

## リリース手順

1. `develop` で開発・コミット
2. リリース時: `master` に merge → `git tag -a vX.Y.Z` → push
3. **`docker-compose.yml.example` の `image:` バージョンを新タグに更新すること**（忘れがち）
4. GitHub Actions が GHCR にイメージをビルド・プッシュ

## ブランチ運用

- `develop`: 開発ブランチ（デフォルト）
- `master`: リリースブランチ

## マイグレーション

- revision ID は `NNN_snake_case` 形式（例: `010_system_role`）
- `down_revision` は前のマイグレーションの `revision` 値と完全一致させること

## テンプレートの注意点

- HTML の `<form>` はネストできない。`<form id="bulkForm">` 内で個別削除が必要な場合は JS で動的に form を生成する
- 取込確認画面（CSV/OFX/Web）は `import_confirm.js` で共通化されている。テンプレートの構造を変える場合は3つとも揃えること

## 勘定科目の system_role

特殊な科目は `system_role` カラムで識別:
- `capital`: 元入金 (3010)
- `retained_earnings`: 繰越利益 (3020)
- `proprietor`: 事業主 (3030)
