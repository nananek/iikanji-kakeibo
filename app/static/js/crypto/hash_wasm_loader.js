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
// - SRI (Subresource Integrity) hash でファイル改ざんを検出
// - hash-wasm は MIT ライセンス + WASM ビルドで決定的、PR #117 dependa での
//   バージョンアップは別 PR でハンドリング

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
