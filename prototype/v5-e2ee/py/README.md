# E2EE Python ベンチマーク — pynacl vs cryptography

本体には統合しない実験ディレクトリ。Epic [#84](https://github.com/nananek/iikanji-kakeibo/issues/84) Phase E0 / Q3 の Python 側検証。

## 目的

`client-py` / `client-tui` で大量仕訳を復号する際の暗号スキーム選定根拠を実測で揃える。

- **AES-256-GCM** (cryptography) — 設計書 §3 の暫定採用
- **ChaCha20-Poly1305** (cryptography) — IETF 標準 (Nonce 96 bits)
- **XChaCha20-Poly1305** (pynacl) — Nonce reuse 耐性高 (192 bits)、設計書 §3 の比較対象

## 実行

```bash
cd server/prototype/v5-e2ee/py
python -m venv .venv && source .venv/bin/activate
pip install cryptography pynacl
python bench.py --n 100000
```

`cryptography` は本体 (`server/`) でも使用中。`pynacl` は本体には入っていないので本ベンチマーク専用に追加。

## 観点

| 観点 | 確認内容 |
|------|---------|
| ops/s | encrypt / decrypt それぞれの 1 秒あたり処理件数 |
| ciphertext_len | 平文 + 認証タグの合計サイズ。AAD なしで 16 B 増 |
| アルゴリズム別の差 | AES-NI が効く CPU では AES が速い。ARM 系では ChaCha20 が拮抗 |
| pynacl のオーバーヘッド | C 拡張呼び出しコストが cryptography (OpenSSL) と比べてどうか |

## 計測記録欄 (実測時に追記)

| CPU | OS | N | AES-256-GCM enc | AES-256-GCM dec | ChaCha20 enc | ChaCha20 dec | XChaCha20 enc | XChaCha20 dec |
|-----|-----|---|-----------------|-----------------|--------------|--------------|---------------|---------------|
| 開発機 (詳細不明、AES-NI 有効と推定) | Debian 13 | 10,000 | 538K ops/s | 563K ops/s | 469K ops/s | 482K ops/s | — | — |
| | | 100,000 | | | | | | |
| | | 1,000,000 | | | | | | |

> 開発機サンプル: 1 仕訳行 78 B / cryptography 44.0.3 / 単一スレッド逐次。AES-NI が効く環境では AES-256-GCM が ChaCha20-Poly1305 に対して 15-17% 程度速い。XChaCha20 (pynacl) は別途要計測。

## 判断材料

設計書 §3 の暫定 (AES-256-GCM) を以下の場合に再評価:

- **XChaCha20-Poly1305 が AES-256-GCM と比べて 50% 以上速い場合** — ChaCha20 系を採用し libsodium.js / pynacl をバンドル追加
- **AES-NI が効かない環境 (Raspberry Pi 等の自家ホスト) で AES が極端に遅い場合** — 同上
- **どちらも僅差の場合** — WebCrypto 標準で済む AES-256-GCM を維持

## 制限事項

- 単一スレッド・逐次計測。並列化 (`asyncio` / `multiprocessing`) は本番想定外なので未対応
- 1 仕訳行相当 (~70 B) のみで計測。証憑画像 (MB 単位) は別途
- メモリ使用量・初期化コストは未計測 (本ベンチマークの対象外)
