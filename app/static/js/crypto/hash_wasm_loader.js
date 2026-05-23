// hash-wasm の動的 CDN ロード (E1 PR-F2)。
//
// ブラウザでパスフレーズ鍵派生に Argon2id を使うため、hash-wasm@4 を ESM
// として動的 import する。ロード後 `globalThis.hashwasm.argon2id` を expose
// するので、argon2.js の resolveBrowserImpl() が自動解決する。
//
// CDN: jsdelivr が GitHub mirror として 100MB/月の使用枠あり、SRI 対応。
// hash-wasm@4.12.0 (2024-12 リリース) は package "argon2id" を含む。
//
// セキュリティ:
// - **本実装は SRI 検証なしで CDN を信頼している**。dynamic import() は
//   `<script integrity="...">` 属性をサポートせず、Import Maps の
//   `integrity` フィールド (Chrome 122+) もブラウザサポートが限定的なため、
//   v5.0 プレビュー段階では「jsdelivr の正当性」を信頼前提としている。
// - CDN MITM / jsdelivr 侵害シナリオでは hash-wasm にバックドアを仕込まれ、
//   Argon2id 派生で MK の元 derived_key を窃取される可能性が残る。
// - 将来の対策候補 (実装は別 PR):
//   1. Import Maps integrity を使う (Chrome 122+ / Firefox 還元待ち)
//   2. hash-wasm@4.12.0 のビルドを `app/static/js/vendor/` に同梱する
//      (バンドラなし制約下でも単純コピーで対応可)
// - hash-wasm は MIT ライセンス。バージョンアップ (dependa の PR 等) は
//   別 PR でハンドリングし、本ファイルの URL も同時更新する。

const HASH_WASM_VERSION = "4.12.0";
// jsdelivr ESM bundle. argon2id を含む単一バンドル
const HASH_WASM_CDN_URL =
  `https://cdn.jsdelivr.net/npm/hash-wasm@${HASH_WASM_VERSION}/dist/index.esm.min.js`;

let _loadPromise = null;

/**
 * hash-wasm を CDN から動的にロードする。複数回呼んでも 1 回のみ実際にロード
 * (同じ Promise を返す)。
 *
 * 戻り値: hash-wasm のモジュール (argon2id 関数等を含む)。
 * 副作用: ロード後、`globalThis.hashwasm = mod` を設定し、`argon2.js` の
 *         resolveBrowserImpl() が自動解決可能になる。
 */
export function loadHashWasm() {
  if (_loadPromise) return _loadPromise;
  _loadPromise = import(/* webpackIgnore: true */ HASH_WASM_CDN_URL)
    .then((mod) => {
      // hash-wasm の default export は無く、named export が直接モジュール
      const exposed = mod.argon2id ? mod : mod.default;
      if (typeof exposed?.argon2id !== "function") {
        throw new Error("hash-wasm loaded but argon2id is not a function");
      }
      globalThis.hashwasm = exposed;
      return exposed;
    })
    .catch((e) => {
      _loadPromise = null; // リトライ可能にする
      throw new Error(`failed to load hash-wasm from CDN: ${e?.message || e}`);
    });
  return _loadPromise;
}

/** テスト用に強制的にキャッシュをクリア。通常は呼ばない。 */
export function _resetHashWasmLoader() {
  _loadPromise = null;
  if ("hashwasm" in globalThis) {
    delete globalThis.hashwasm;
  }
}
