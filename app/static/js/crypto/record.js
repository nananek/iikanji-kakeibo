// Phase E3: 仕訳 / 仕訳明細 / 医療費の record-level 暗号化 helper。
//
// 1 レコード = 1 JSON 暗号文 (JSON-then-encrypt) パターン。AAD には
// テーブル種別 + user_id を big-endian で連結する (Option B、§12.2 設計判断)。
//
// AAD swap 攻撃の検知能力:
//   - 異テーブル間 (je vs jel vs me) ですり替えても tableType プレフィックスで検知
//   - 異ユーザー間ですり替えても user_id で検知
//   - **同一ユーザー / 同一テーブル内の entry-to-entry swap は検知しない** (Option B)
//
// Option B トレードオフ:
//   - 新規 POST 時に entry_id が未確定でも AAD を構築できる (1 RTT)
//   - swap 攻撃はサーバ侵害が前提で、その時点で他の攻撃ベクターも開いている
//
// balance_cache_blobs (`bcb`) は (year, period) で論理 ID が決まる特殊例で、
// クライアントが PUT 時に (year, period) を知っているため AAD に含める。
//
// 設計書 §12.2 (AAD フォーマット) 参照。


// --- AAD 構築 ---


/** uint64 を 8B big-endian Uint8Array に変換。 */
export function uint64BE(n) {
  // Number は 53bit までしか安全ではない。それ超過の Number を受けると
  // BigInt(n) する前に Number 側で精度が失われ、誤った値で encode される。
  // 64bit ID を扱うときは BigInt を直接渡してもらう契約。
  if (typeof n === "number" && !Number.isSafeInteger(n)) {
    throw new RangeError(
      `uint64BE: Number precision loss, pass BigInt instead: ${n}`,
    );
  }
  const big = typeof n === "bigint" ? n : BigInt(n);
  if (big < 0n || big > 0xFFFF_FFFF_FFFF_FFFFn) {
    throw new RangeError(`uint64BE: out of range: ${n}`);
  }
  const out = new Uint8Array(8);
  const view = new DataView(out.buffer);
  // setBigUint64 は ES2020 / すべての主要ブラウザで対応
  view.setBigUint64(0, big, /* littleEndian */ false);
  return out;
}


function _concat(...arrays) {
  let len = 0;
  for (const a of arrays) len += a.byteLength;
  const out = new Uint8Array(len);
  let offset = 0;
  for (const a of arrays) {
    out.set(a, offset);
    offset += a.byteLength;
  }
  return out;
}


const TEXT_ENC = new TextEncoder();
const NUL = TEXT_ENC.encode("\0");


// テーブル種別ごとの ids 個数。間違った数を渡すと別の (しかし有効な) AAD が
// 生成され、暗号化した BLOB が永遠に復号できなくなるリスクがあるため厳密検査。
//
// je / jel / me は Option B で 0 個 (user_id のみで一意)。
// bcb は (year, period) を含む 1 個 (year*100 + period) のまま。
//
// E4 (#111) 証憑画像は voucher_id を含む 1 個。画像は件数が少なく 1 件ずつ
// 2 段階 upload (init で採番 → AAD 束縛) するため、entry-to-entry swap も
// voucher_id で検知できる (journal の Option B より強い束縛)。
//   vimg   = 画像本体        (id: voucher_id)
//   vthumb = サムネイル      (id: voucher_id)
//   vmeta  = メタ情報 JSON   (id: voucher_id)
//   valog  = 監査ログ detail (id: voucher_id) ※ PR-D で使用
const TABLE_ID_COUNT = {
  je: 0,    // journal_entries (user_id のみ)
  jel: 0,   // journal_entry_lines (user_id のみ)
  me: 0,    // medical_expenses (user_id のみ)
  bcb: 1,   // balance_cache_blobs (year*100+period)
  vimg: 1,  // vouchers 画像本体 (voucher_id)
  vthumb: 1, // vouchers サムネイル (voucher_id)
  vmeta: 1, // vouchers メタ情報 (voucher_id)
  valog: 1, // voucher_audit_logs detail (voucher_id)
};


/**
 * AAD バイト列を構築。
 *
 * @param {"je"|"jel"|"me"|"bcb"|"vimg"|"vthumb"|"vmeta"|"valog"} tableType
 *   テーブル種別プレフィックス
 *   - "je"  = journal_entries (ids: なし)
 *   - "jel" = journal_entry_lines (ids: なし)
 *   - "me"  = medical_expenses (ids: なし)
 *   - "bcb" = balance_cache_blobs (ids: year*100+period)
 *   - "vimg"/"vthumb"/"vmeta"/"valog" = vouchers 系 (ids: voucher_id)
 * @param {number|bigint} userId
 * @param {Array<number|bigint>} ids  追加識別 ID 列 (bcb / vouchers 系で使用)
 * @returns {Uint8Array}
 */
export function buildAAD(tableType, userId, ...ids) {
  if (!Object.prototype.hasOwnProperty.call(TABLE_ID_COUNT, tableType)) {
    throw new Error(`buildAAD: unsupported tableType: ${tableType}`);
  }
  const expectedCount = TABLE_ID_COUNT[tableType];
  if (ids.length !== expectedCount) {
    throw new Error(
      `buildAAD: ${tableType} expects ${expectedCount} id(s), got ${ids.length}`,
    );
  }
  const parts = [TEXT_ENC.encode(tableType), NUL, uint64BE(userId)];
  for (const id of ids) {
    parts.push(NUL, uint64BE(id));
  }
  return _concat(...parts);
}


// --- encrypt / decrypt ---


/**
 * record (plain object) を JSON 化 → MK で AES-GCM 暗号化。
 *
 * @param {Object} client            SharedCryptoClient
 * @param {Object} record            シリアライズ対象の plain object
 *   - 内部に {v: 1, ...} を含めること推奨 (将来のスキーマ進化)
 * @param {Uint8Array} aad
 * @returns {Promise<{blob: Uint8Array, iv: Uint8Array}>}
 *   blob = ciphertext + 16B GCM tag, iv = 12B random
 */
export async function encryptRecord(client, record, aad) {
  if (!client || typeof client.encrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  if (!aad || !(aad instanceof Uint8Array)) {
    throw new Error("aad must be a Uint8Array");
  }
  const json = JSON.stringify(record);
  const plaintext = TEXT_ENC.encode(json);
  const res = await client.encrypt(plaintext, aad);
  return { blob: res.ciphertext, iv: res.iv };
}


/**
 * blob + iv + aad を復号 → JSON parse して record を返す。
 *
 * @param {Object} client
 * @param {Uint8Array} blob
 * @param {Uint8Array} iv
 * @param {Uint8Array} aad
 * @returns {Promise<Object>}
 *   AAD すり替えで GCM tag 検証に失敗 → SharedCryptoClient が throw する。
 */
export async function decryptRecord(client, blob, iv, aad) {
  if (!client || typeof client.decrypt !== "function") {
    throw new Error("client (SharedCryptoClient) is required");
  }
  const res = await client.decrypt(blob, iv, aad);
  const json = new TextDecoder().decode(res.plaintext);
  // plaintext Uint8Array は明示的にゼロ埋め (worker 側も Transferable detach 済)。
  // ただし json (string) は JS の言語仕様上不変でゼロ化不可能なため、GC まで
  // メモリに残り得る制約がある (これは JS の言語制約で回避手段なし)。
  try { res.plaintext.fill(0); } catch (_e) { /* ignore */ }
  return JSON.parse(json);
}
