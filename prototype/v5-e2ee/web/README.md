# E2EE プロトタイプ — WebCrypto AES-GCM + Worker 隔離

本体には統合しない実験ディレクトリ。Epic [#84](https://github.com/nananek/iikanji-kakeibo/issues/84) Phase E0 の検証用。

## 目的

設計書 §5 の以下の判断を実機で確認する:

- AES-256-GCM (WebCrypto 標準) で家計簿用途の暗号化が成立するか
- Master Key を Web Worker クロージャ内のみに保持し、`window.*` へ露出させずに encrypt/decrypt できるか (Q12)
- postMessage で送られてきた不正型を Worker が安全に弾けるか (Q12)
- 大量データの暗号化/復号レイテンシ (Q3 の Web 版相当)

## 起動

WebCrypto は HTTPS or `localhost` でしか動かない。任意の静的サーバで `prototype/v5-e2ee/web/` を配信する:

```bash
cd server/prototype/v5-e2ee/web
python3 -m http.server 8765
# → http://localhost:8765/ を開く
```

## 観点

| セクション | 確認内容 |
|----------|----------|
| 1. Master Key 生成 | `generateKey()` で 32B 鍵が Worker 内で生成・保持される。**rawKey はメインスレッドに渡さない** (Q12 の中核命題)。import 後は Worker 内のバッファをゼロ埋め |
| 2. 単一暗号化/復号 | 12B IV + ciphertext (= plaintext_len + 16B tag) のサイズ膨張を観察 |
| 3. 大量ベンチマーク | N=10K / 100K / 1M で encrypt/decrypt の ops/s を計測。設計書 §3 の Nonce 2^32 警告ラインとの距離感を確認 |
| 4. 不正メッセージ耐性 | 非オブジェクト / 不足フィールド / 型違いを送りつけ、Worker がクラッシュせず `{ok: false, error}` を返すこと |
| 5. window 露出チェック | グローバルに鍵関連の名前が漏れていないこと |

## ベンチマーク記録欄 (実測時に追記)

| ブラウザ | OS / CPU | N | encrypt (ops/s) | decrypt (ops/s) | 備考 |
|--------|---------|---|----------------|-----------------|------|
| | | 10,000 | | | |
| | | 100,000 | | | |
| | | 1,000,000 | | | |

## Q12 fuzz 結果欄 (実測時に追記)

| 入力 | 期待: ok=false (or worker 無反応) | 実測 |
|-----|--------------------------------|------|
| `null` | | |
| `"string"` | | |
| `42` | | |
| `{id:1, type:"encrypt"}` (plaintext 欠如) | | |
| `{id:2, type:"decrypt", ciphertext:"not-uint8array"}` | | |
| `{id:3, type:"setKey", rawKey: Uint8Array(16)}` (32B 期待) | | |
| `{id:4, type:"unknown"}` | | |

## 利用ガイド

### `setKey(rawKey)` の呼び出し側責務

`setKey` は `postMessage` の structured clone で `rawKey` のコピーを Worker に
渡し、Worker 側では `importMasterKey` 後に `fill(0)` でゼロ埋めする。一方で
**メインスレッド側の元バッファは Worker からは触れない** ので、呼び出し元が
明示的にゼロ埋めする必要がある:

```js
const raw = await deriveFromPassphrase(...); // 例: Argon2id 出力
await client.setKey(raw);
raw.fill(0); // 呼び出し側の責務
```

Argon2id / WebAuthn PRF 連携を実装する際の必須手順。

## 制限事項

- このプロトタイプは Argon2id を扱わない (パスフレーズ → MK 派生は別途 `argon2-browser` バンドルで実測する)
- WebAuthn PRF からの鍵派生も別途 (この prototype は MK を直接生成)
- IndexedDB への暗号文保存・Service Worker 連携も対象外
- `CryptoClient.worker` は fuzz セクションから直接アクセスするため public のまま。
  本実装では `#worker` (private field) にして外部からの postMessage を防ぐ想定
- §5 の window 露出チェックは固定リスト方式。本実装では `Object.keys(window)` の
  ベースライン差分を取る等で網羅性を上げる想定
