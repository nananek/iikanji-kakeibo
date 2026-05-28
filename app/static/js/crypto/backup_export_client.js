// Phase v5 BU-1: 全データバックアップ export クライアント。
//
// `GET /api/v1/backup/export` で取得した暗号文を含む JSON を、
// 本人 MK で復号して **平文の plain object** に変換する純粋関数群。
//
// サーバから返る JSON 構造 (v1.0):
//   {
//     version: "1.0", exported_at, user_id,
//     data: {
//       accounts: [{code, name, ...}],         // 暗号化なし
//       fiscal_closes: [{year, closed_period}],// 暗号化なし
//       journal_entries: [{id, encrypted_blob, blob_iv, ...旧平文}],
//       journal_entry_lines: [{id, journal_entry_id, encrypted_blob, ...}],
//       medical_expenses: [{id, encrypted_blob, ...}],
//       balance_cache_blobs: [{year, period, encrypted_blob, blob_iv, ...}],
//       vouchers: [{id, image_key, image_mime, image_data (base64), ...}],
//     }
//   }
//
// vouchers の画像 (image_data) はサーバ側ストレージに平文保存されている
// ため復号不要。本クライアントではメタ + base64 をそのままパススルーする。
//
// 復号後の構造 (本関数の戻り値):
//   data.journal_entries[i]: 復号できた行は body フィールドを展開 (date /
//     description / source / batch_id / fiscal_period が暗号文由来)。
//     復号できない行 (旧平文のみ) はサーバが返した値そのまま。
//   data.journal_entry_lines[i]: 同上。
//   data.medical_expenses[i]: 同上。
//   data.balance_cache_blobs[i]: encrypted_blob を復号して `cumulative` フィールドに展開
//     ({code: [debit, credit], ...} の JSON object)。
//
// 復号失敗は当該行のみ局所化 (1 件の失敗で全件 reject はしない)。
// 失敗行は元の暗号文情報をそのまま残し、`_decryptError` フィールドに理由を記録する。

import { b64decode } from "./b64.js";
import { buildAAD, decryptRecord } from "./record.js";


function _periodKey(year, period) {
  return year * 100 + period;
}


async function _decryptOne(client, userId, blob_b64, iv_b64, aadBuilder) {
  if (!blob_b64 || !iv_b64) return { ok: false, body: null, err: null };
  try {
    const blob = b64decode(blob_b64);
    const iv = b64decode(iv_b64);
    const aad = aadBuilder();
    const body = await decryptRecord(client, blob, iv, aad);
    return { ok: true, body, err: null };
  } catch (e) {
    return { ok: false, body: null, err: e?.message || String(e) };
  }
}


/**
 * サーバから取得した backup JSON を本人 MK で復号する。
 *
 * @param {Object} client     SharedCryptoClient
 * @param {Object} backup     /api/v1/backup/export のレスポンス JSON そのまま
 * @returns {Promise<Object>} 復号済み backup (同じ shape + 各 row の body 展開)
 */
export async function decryptBackup(client, backup) {
  if (!backup || typeof backup !== "object") {
    throw new TypeError("backup must be an object");
  }
  if (!backup.data || typeof backup.data !== "object") {
    throw new TypeError("backup.data must be an object");
  }
  const userId = backup.user_id;
  if (typeof userId !== "number") {
    throw new TypeError("backup.user_id must be a number");
  }

  const out = {
    version: backup.version,
    exported_at: backup.exported_at,
    user_id: userId,
    data: {
      accounts: backup.data.accounts || [],
      fiscal_closes: backup.data.fiscal_closes || [],
      journal_entries: [],
      journal_entry_lines: [],
      medical_expenses: [],
      balance_cache_blobs: [],
      // 画像はサーバ側ストレージに平文保存、復号不要のためそのまま通す
      vouchers: backup.data.vouchers || [],
      // AIDraft も画像 + メタをそのまま (BU-2b)
      ai_drafts: backup.data.ai_drafts || [],
      // UserAIConfig は api_key_blob (暗号文) を含む。本クライアントでは
      // 復号せずパススルー (リストア時に MK が必要なため)。
      user_ai_config: backup.data.user_ai_config || null,
      webhook_configs: backup.data.webhook_configs || [],
      tax_form_mappings: backup.data.tax_form_mappings || [],
      csv_column_profiles: backup.data.csv_column_profiles || [],
    },
  };

  for (const e of backup.data.journal_entries || []) {
    const r = await _decryptOne(
      client, userId, e.encrypted_blob, e.blob_iv,
      () => buildAAD("je", userId),
    );
    const row = { ...e };
    delete row.encrypted_blob;
    delete row.blob_iv;
    if (r.ok && r.body) {
      Object.assign(row, r.body);
    } else if (r.err) {
      row._decryptError = r.err;
    }
    out.data.journal_entries.push(row);
  }

  for (const l of backup.data.journal_entry_lines || []) {
    const r = await _decryptOne(
      client, userId, l.encrypted_blob, l.blob_iv,
      () => buildAAD("jel", userId),
    );
    const row = { ...l };
    delete row.encrypted_blob;
    delete row.blob_iv;
    if (r.ok && r.body) {
      Object.assign(row, r.body);
    } else if (r.err) {
      row._decryptError = r.err;
    }
    out.data.journal_entry_lines.push(row);
  }

  for (const m of backup.data.medical_expenses || []) {
    const r = await _decryptOne(
      client, userId, m.encrypted_blob, m.blob_iv,
      () => buildAAD("me", userId),
    );
    const row = { ...m };
    delete row.encrypted_blob;
    delete row.blob_iv;
    if (r.ok && r.body) {
      Object.assign(row, r.body);
    } else if (r.err) {
      row._decryptError = r.err;
    }
    out.data.medical_expenses.push(row);
  }

  for (const b of backup.data.balance_cache_blobs || []) {
    const r = await _decryptOne(
      client, userId, b.encrypted_blob, b.blob_iv,
      () => buildAAD("bcb", userId, _periodKey(b.year, b.period)),
    );
    const row = {
      year: b.year, period: b.period, updated_at: b.updated_at,
    };
    if (r.ok && r.body) {
      row.cumulative = r.body;
    } else if (r.err) {
      row._decryptError = r.err;
    }
    out.data.balance_cache_blobs.push(row);
  }

  return out;
}
