# Contributing to いいかんじ™家計簿

このプロジェクトへの貢献に興味を持っていただきありがとうございます。

## ライセンス

本サーバー本体は [Sustainable Use License v1.0](./LICENSE) のもとで配布
されています。別途の Contributor License Agreement (CLA) 文書の締結は
現時点では要求していませんが、リポジトリへ Pull Request を提出すること
で、あなたの貢献を同ライセンスのもとで本プロジェクトに提供することに
同意したものとみなされます (Developer Certificate of Origin 相当の運用)。
必要に応じて将来明示的な CLA が追加される可能性があります。

## 貢献の流れ

1. Issue を立てて変更内容を議論する (大きな変更の場合は事前に推奨)
2. `develop` ブランチから feature ブランチを切る (`feat/*`, `fix/*`, `chore/*` 等)
3. テストを追加・更新する (新規 service / view を書いたら同 PR でテスト網羅、
   既存カバレッジ 94% を下回らないことを目安)
4. CI (GitHub Actions: pytest + Playwright E2E + CodeQL) を緑にする
5. Pull Request を作成し、`develop` をベースに提出する
6. レビューに対応し、squash merge で取り込まれる

## コーディング規約

- Python 3.12 / Flask 3.x / SQLAlchemy 2.x
- マイグレーションは `NNN_snake_case` 形式
- テンプレートの共通化は `_partials/` ディレクトリで管理
- 新規エンドポイントには `@login_required` を忘れない
- AI / 決済 / メール等の外部連携は抽象インターフェース + 実装の分離を維持

## 公開ロードマップ

v4.0 系列のメインの取り組みは Epic [#64](https://github.com/nananek/iikanji-kakeibo/issues/64) で
管理されています。新規参加の場合は、関連 Issue を確認してから着手すると
スムーズです。

## 質問・相談

GitHub Issues / Discussions でお気軽にどうぞ。
