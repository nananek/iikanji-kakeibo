// hash-wasm の動的ロード (E1 PR-F2)。
//
// ブラウザでパスフレーズ鍵派生に Argon2id を使うため、hash-wasm@4 を ESM
// として動的 import する。ロード後 `globalThis.hashwasm.argon2id` を expose
// するので、argon2.js の resolveBrowserImpl() が自動解決する。
//
// セキュリティ:
// - **CDN ではなく同 origin (vendor) から配信する**。Argon2id という MK 派生
//   を担うライブラリを CDN から無検証で読むと、CDN MITM / プロバイダ侵害で
//   MK 漏洩リスクを負う。dynamic import() は SRI を提供できないため、
//   同 origin 配信が最も確実 (同 origin 改変には認証済みデプロイが必要)。
// - vendor ファイル: app/static/js/vendor/hash-wasm-<VER>.esm.min.js
// - SHA-384 と更新手順は app/static/js/vendor/README.md
// - hash-wasm は MIT。LICENSE-hash-wasm を同梱
//
// バージョンアップ:
// - vendor ファイル名 + 本ファイルの HASH_WASM_VERSION + README の SHA-384
//   の 3 点を同時更新する

const HASH_WASM_VERSION = "4.12.0";
// 同 origin 配信。Flask の static ルートから配信される
const HASH_WASM_URL =
  `/static/js/vendor/hash-wasm-${HASH_WASM_VERSION}.esm.min.js`;

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
  _loadPromise = import(/* webpackIgnore: true */ HASH_WASM_URL)
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
      throw new Error(`failed to load hash-wasm: ${e?.message || e}`);
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
