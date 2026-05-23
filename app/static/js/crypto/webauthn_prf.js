// WebAuthn PRF 拡張からの derived_key 派生 (E1 PR-F1)。
// 設計書 §2 / §10.3 (Passkey PRF からの鍵派生)。
//
// フロー:
// 1. navigator.credentials.get(...) を呼ぶ際に
//    publicKey.extensions.prf.eval.first = UTF-8("iikanji-master-key-v1")
//    を指定して PRF 評価をリクエスト
// 2. credential.getClientExtensionResults().prf.results.first に 32B が返る
// 3. その 32B を HKDF-SHA256(input=PRF出力, salt=zero, info="iikanji-master-key-v1")
//    で derived_key (32B) に派生
//
// 既存 app/static/js/webauthn.js (Passkey 登録・認証 UI) には PRF 拡張は
// 含まれていないため、本モジュールは E1 鍵管理基盤の独立レイヤとして提供する。
// PR-F2 でウィザード UI から呼び出す想定。

// 重要: PRF_EVAL_INPUT と HKDF_INFO は意図的に同値 "iikanji-master-key-v1"。
//   - PRF_EVAL_INPUT: WebAuthn 認証時に PRF 拡張に渡す入力 (authenticator が
//     credentialId + これを元に決定的に 32B を返す)
//   - HKDF_INFO: PRF 出力を 32B derived_key に派生する際の HKDF info コンテキスト
// 同じ文字列を使うことで「マスター鍵用途」のドメイン分離を一意化する。
// 将来別用途 (例: AuditPackage 暗号化用の鍵派生) を追加する場合は **両方を**
// 別文字列に変更すること (info だけ変えても PRF eval は credentialId に依存
// するため、PRF 出力自体は同じになる → ドメイン分離が崩れる)。
const HKDF_INFO = "iikanji-master-key-v1";
const PRF_EVAL_INPUT = "iikanji-master-key-v1";

/** PRF eval 入力 (32B 未満でもブラウザが内部で展開する。文字列 → UTF-8) */
export function getPrfEvalInputBytes() {
  return new TextEncoder().encode(PRF_EVAL_INPUT);
}

/**
 * navigator.credentials.get の `publicKey.extensions` に渡す PRF 拡張の値を構築。
 * 呼び出し側 (Passkey 認証ロジック) はこの結果を extensions に組み込んで
 * `navigator.credentials.get({ publicKey: { ..., extensions: this } })` する。
 *
 * @returns {Object} `{ prf: { eval: { first: Uint8Array } } }`
 */
export function buildPrfExtensionInput() {
  return {
    prf: {
      eval: {
        first: getPrfEvalInputBytes(),
      },
    },
  };
}

/**
 * Credential.getClientExtensionResults() から PRF の 32B 出力を取り出す。
 * PRF 非対応端末 (results.first === undefined) は null を返す → 呼び出し側で
 * パスフレーズフォールバックを案内する。
 *
 * @param {PublicKeyCredential|Object} credential  navigator.credentials.get の戻り値
 * @returns {Uint8Array|null}
 */
export function extractPrfOutput(credential) {
  const results = typeof credential?.getClientExtensionResults === "function"
    ? credential.getClientExtensionResults()
    : credential?.clientExtensionResults; // テスト用の素オブジェクト互換
  const first = results?.prf?.results?.first;
  if (!first) return null;
  // ArrayBuffer / Uint8Array いずれかで返るのでバイト列に正規化
  if (first instanceof Uint8Array) return new Uint8Array(first);
  if (first instanceof ArrayBuffer) return new Uint8Array(first);
  // Some WebAuthn polyfills return Array
  if (Array.isArray(first)) return new Uint8Array(first);
  throw new Error("unsupported prf.results.first type");
}

/**
 * PRF 出力 (32B 想定) を HKDF-SHA256 で 32B derived_key に派生。
 * info パラメータでドメイン分離 (将来 AuditPackage 暗号化用に別 info 派生も可)。
 *
 * @param {Uint8Array} prfOutput  PRF results.first
 * @param {Object} [opts]
 * @param {string} [opts.info]    HKDF info (デフォルト: "iikanji-master-key-v1")
 * @returns {Promise<Uint8Array>} 32B derived_key
 */
export async function deriveKeyFromPrfOutput(prfOutput, opts = {}) {
  if (!(prfOutput instanceof Uint8Array) || prfOutput.byteLength === 0) {
    throw new Error("prfOutput must be non-empty Uint8Array");
  }
  const info = opts.info ?? HKDF_INFO;
  const salt = new Uint8Array(32); // all-zero (設計書既定)
  const ikm = await crypto.subtle.importKey(
    "raw", prfOutput, { name: "HKDF" }, false, ["deriveBits"],
  );
  const derived = await crypto.subtle.deriveBits(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt,
      info: new TextEncoder().encode(info),
    },
    ikm,
    256,
  );
  return new Uint8Array(derived);
}

/**
 * 一連の Passkey 認証で PRF を評価して derived_key を返す高レベル API。
 * `navigator.credentials.get(...)` 呼び出しは呼び出し側で行い、その戻り値を渡す
 * (ユースケースによって allowCredentials / userVerification 等を制御するため)。
 *
 * @param {PublicKeyCredential|Object} credential
 * @returns {Promise<Uint8Array|null>} 32B derived_key、PRF 非対応なら null
 */
export async function deriveKeyFromCredential(credential) {
  const prf = extractPrfOutput(credential);
  if (prf === null) return null;
  try {
    return await deriveKeyFromPrfOutput(prf);
  } finally {
    prf.fill(0); // raw PRF 出力は派生後に即ゼロ埋め
  }
}
