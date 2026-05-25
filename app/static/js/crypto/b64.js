// Base64 encode/decode for Uint8Array <-> string.
//
// Web 標準の btoa/atob は文字列専用なので、バイナリ用のラッパを提供する。
// 各 orchestrator (ai_journal / voucher_attach / web_import / reconcile /
// suggest_categories / csv_columns_detect / ai_config_form / ai_analyze) で
// 同実装が重複していたものを集約。


export function b64encode(bytes) {
  let s = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s);
}


export function b64decode(s) {
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
