// LLM レスポンスの JSON 抽出ユーティリティ (E2 PR-C-1)。
//
// サーバ側 ai_receipt.py の `_extract_json` と等価の挙動。LLM がコードブロック
// や前後余白付きで JSON を返すケースに対応する。

/**
 * テキストから JSON オブジェクトを抽出してパース。
 *
 * 試行順:
 *   1. 全体をそのまま JSON.parse
 *   2. ```json ... ``` ブロック内
 *   3. 最初に出現する {...} ブロック
 *
 * 全部失敗したら SyntaxError を投げる。
 */
export function extractJson(text) {
  if (typeof text !== "string") {
    throw new TypeError("extractJson: text must be string");
  }
  const trimmed = text.trim();
  // 1. そのまま
  try {
    return JSON.parse(trimmed);
  } catch (_e) { /* fallthrough */ }
  // 2. ```json ... ``` ブロック
  const fence = trimmed.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
  if (fence) {
    return JSON.parse(fence[1]);
  }
  // 3. 最初の {...}
  const obj = trimmed.match(/\{[\s\S]*\}/);
  if (obj) {
    return JSON.parse(obj[0]);
  }
  throw new SyntaxError("extractJson: no JSON found in text");
}


/** Uint8Array → base64 (Node + ブラウザ両対応)。 */
export function bytesToBase64(bytes) {
  if (typeof Buffer !== "undefined") {
    return Buffer.from(bytes).toString("base64");
  }
  let s = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s);
}
