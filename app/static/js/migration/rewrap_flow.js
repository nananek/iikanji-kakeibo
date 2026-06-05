// E7 (#114) クライアント側 temp-MK 再ラップフロー (再ラップフロー PR-3)。
//
// サーバが移行時に利用者ごとの temp-MK で全データを暗号化した暫定状態 (§16.4) を、
// クライアントが「temp-MK で復号 → 本物 MK で再暗号化 → blob 差し替え」して真の
// E2EE に移行する。再ラップは AAD と平文を不変に保ち、鍵 (temp-MK→本物 MK) と IV
// のみを変える。平文は Worker 内に閉じ込められる (rewrap op)。
//
// 冪等性・再開: 既に本物 MK 済の blob は temp-MK で復号できない (GCM 認証失敗) ため
// rewrapRecord/rewrapBlob が reject する。これを「再ラップ済」とみなして skip する
// ことで、進捗スキーマを増やさずに中断・再実行に耐える (失敗停止 + 再実行で継続)。
//
// 認証: temp-mk / rewrap / rewrap-image / finalize はすべてセッション限定
// (Bearer 不可)。fetch は credentials:include。/api/v1 は CSRF 免除。
//
// テスト容易性のため fetch / post / 各 import を DI 可能にしている
// (test_migration_rewrap.mjs は実 WebCrypto + 偽 fetch で end-to-end 検証)。

import { buildAAD, rewrapRecord, rewrapBlob } from "../crypto/record.js";
import { b64encode, b64decode } from "../crypto/b64.js";


const REWRAP_BATCH = 200;   // POST /migration/rewrap の 1 バッチ件数 (サーバ上限 500)
const VOUCHER_PAGE = 200;   // voucher-blobs の per_page (サーバ上限 200)


function _chunk(arr, n) {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}


async function _getJson(fetchImpl, url) {
  const r = await fetchImpl(url, { credentials: "include" });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`GET ${url}: HTTP ${r.status} ${e.error || ""}`);
  }
  return r.json();
}


async function _postJson(fetchImpl, url, body, method = "POST") {
  const r = await fetchImpl(url, {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const e = await r.json().catch(() => ({}));
    throw new Error(`${method} ${url}: HTTP ${r.status} ${e.error || ""}`);
  }
  return r.json();
}


/**
 * record 形式 (blob + 別 iv) の項目群を temp-MK→本物 MK で再ラップする。
 *
 * @param {Object} client  setRewrapKey 済の SharedCryptoClient
 * @param {Array} rawItems  [{key: {id}|{year,period}, blobB64, ivB64, aad}]
 * @returns {Promise<{items: Array, skipped: number}>}
 *   items = [{...key, encrypted_blob, blob_iv}] (POST 用、再ラップ済のみ)
 *   skipped = temp-MK 復号失敗 (= 既に本物 MK 済) の件数
 */
export async function rewrapRecordItems(client, rawItems) {
  const items = [];
  let skipped = 0;
  for (const it of rawItems) {
    // base64 decode の失敗はサーバ応答の破損 (= 異常) であり、「再ラップ済 skip」
    // とは性質が異なる。これを silently skip すると、未再ラップのまま finalize で
    // temp_mk が消去され、その blob が恒久的に復号不能になりうる。よって decode
    // 失敗は throw して移行全体を中断する (temp_mk は保持されるので安全に再実行
    // 可能)。skip は temp-MK 復号失敗 (= 既に本物 MK 済) のみに限定する。
    const blob = b64decode(it.blobB64);
    const iv = b64decode(it.ivB64);
    try {
      const re = await rewrapRecord(client, blob, iv, it.aad);
      items.push({
        ...it.key,
        encrypted_blob: b64encode(re.blob),
        blob_iv: b64encode(re.iv),
      });
    } catch (_e) {
      // temp-MK で復号できない = 既に本物 MK で再ラップ済 → skip (冪等)。
      skipped++;
    }
  }
  return { items, skipped };
}


/** rewrap した items を table 指定でバッチ POST する。 */
async function _postRewrap(fetchImpl, table, items) {
  let updated = 0;
  for (const batch of _chunk(items, REWRAP_BATCH)) {
    const res = await _postJson(
      fetchImpl, "/api/v1/migration/rewrap", { table, items: batch },
    );
    updated += res.updated || 0;
  }
  return updated;
}


/**
 * 仕訳 (je) + 明細 (jel) を年度ごとに再ラップする。GET /journals をページ走査し、
 * entry blob と line blob を rewrap して table=je / jel で差し替える。
 */
export async function rewrapJournalsForYear({ client, userId, year, fetchImpl }) {
  const aadJe = buildAAD("je", userId);
  const aadJel = buildAAD("jel", userId);
  let page = 1;
  let total = 0;
  let counts = { je: 0, jel: 0, jeSkipped: 0, jelSkipped: 0 };
  for (;;) {
    const body = await _getJson(
      fetchImpl,
      `/api/v1/journals?fiscal_year=${year}&page=${page}&per_page=100`,
    );
    const entries = body.journals || [];
    total = body.total || 0;

    const jeRaw = [];
    const jelRaw = [];
    for (const e of entries) {
      // closing 仕訳は encrypted_blob 空のセンチネルがありうる → blob 無しは skip。
      if (e.encrypted_blob && e.blob_iv) {
        jeRaw.push({
          key: { id: e.id }, blobB64: e.encrypted_blob,
          ivB64: e.blob_iv, aad: aadJe,
        });
      }
      for (const l of e.lines || []) {
        if (l.encrypted_blob && l.blob_iv) {
          jelRaw.push({
            key: { id: l.id }, blobB64: l.encrypted_blob,
            ivB64: l.blob_iv, aad: aadJel,
          });
        }
      }
    }

    const je = await rewrapRecordItems(client, jeRaw);
    const jel = await rewrapRecordItems(client, jelRaw);
    counts.je += await _postRewrap(fetchImpl, "je", je.items);
    counts.jel += await _postRewrap(fetchImpl, "jel", jel.items);
    counts.jeSkipped += je.skipped;
    counts.jelSkipped += jel.skipped;

    if (entries.length === 0 || page * 100 >= total) break;
    page++;
  }
  return counts;
}


/** 残高キャッシュ (bcb) を年度ごとに再ラップする。AAD id = year*100+period。 */
export async function rewrapBalanceCacheForYear({ client, userId, year, fetchImpl }) {
  const body = await _getJson(
    fetchImpl, `/api/v1/balance-cache-blobs?year=${year}`,
  );
  const raw = (body.blobs || []).map((b) => ({
    key: { year: b.year, period: b.period },
    blobB64: b.encrypted_blob,
    ivB64: b.blob_iv,
    aad: buildAAD("bcb", userId, year * 100 + b.period),
  }));
  const { items, skipped } = await rewrapRecordItems(client, raw);
  const updated = await _postRewrap(fetchImpl, "bcb", items);
  return { bcb: updated, bcbSkipped: skipped };
}


/** 医療費 (me) を全件再ラップする (年度フィルタなしで全取得)。 */
export async function rewrapMedicalExpenses({ client, userId, fetchImpl }) {
  const aad = buildAAD("me", userId);
  const body = await _getJson(fetchImpl, "/api/v1/medical-expenses");
  const raw = (body.expenses || [])
    .filter((m) => m.encrypted_blob && m.blob_iv)
    .map((m) => ({
      key: { id: m.id }, blobB64: m.encrypted_blob, ivB64: m.blob_iv, aad,
    }));
  const { items, skipped } = await rewrapRecordItems(client, raw);
  const updated = await _postRewrap(fetchImpl, "me", items);
  return { me: updated, meSkipped: skipped };
}


/**
 * 1 証憑の画像 (vimg) + サムネ (vthumb) を再ラップして PUT rewrap-image する。
 * 画像は inline-iv (iv‖ct‖tag)。aad_id を AAD id に使う (voucher_id でない)。
 * @returns {Promise<boolean>} 再ラップして送信したら true、skip したら false
 */
export async function rewrapVoucherImage({
  client, userId, voucher, fetchImpl,
}) {
  const aadId = BigInt(voucher.aad_id);
  const aadImg = buildAAD("vimg", userId, aadId);

  const imgRes = await fetchImpl(
    `/api/v1/migration/voucher-image/${voucher.id}`, { credentials: "include" },
  );
  if (!imgRes.ok) {
    throw new Error(`voucher-image ${voucher.id}: HTTP ${imgRes.status}`);
  }
  const imgBytes = new Uint8Array(await imgRes.arrayBuffer());
  let imageCt;
  try {
    imageCt = await rewrapBlob(client, imgBytes, aadImg);
  } catch (_e) {
    // temp-MK で復号不可 = 既に再ラップ済 → 画像/サムネとも skip。
    return false;
  }

  const payload = { voucher_id: voucher.id, image_ct: b64encode(imageCt) };

  if (voucher.has_thumbnail) {
    const aadThumb = buildAAD("vthumb", userId, aadId);
    const thRes = await fetchImpl(
      `/api/v1/migration/voucher-image/${voucher.id}?size=thumb`,
      { credentials: "include" },
    );
    if (thRes.ok) {
      const thBytes = new Uint8Array(await thRes.arrayBuffer());
      try {
        const thumbCt = await rewrapBlob(client, thBytes, aadThumb);
        payload.thumb_ct = b64encode(thumbCt);
      } catch (_e) {
        // サムネが既に再ラップ済/復号不可なら本体のみ送る。
      }
    }
  }

  await _postJson(fetchImpl, "/api/v1/migration/rewrap-image", payload, "PUT");
  return true;
}


/**
 * 証憑のメタ (vmeta) / 監査ログ (valog) / 画像 (vimg/vthumb) を再ラップする。
 * voucher-blobs をページ走査し、メタ・ログは rewrap API、画像は rewrap-image API。
 */
export async function rewrapVouchers({ client, userId, fetchImpl, onProgress }) {
  let page = 1;
  let total = 0;
  const counts = { vmeta: 0, valog: 0, vimg: 0, vmetaSkipped: 0, valogSkipped: 0 };
  for (;;) {
    const body = await _getJson(
      fetchImpl,
      `/api/v1/migration/voucher-blobs?page=${page}&per_page=${VOUCHER_PAGE}`,
    );
    const vouchers = body.vouchers || [];
    total = body.total || 0;

    const metaRaw = [];
    const logRaw = [];
    for (const v of vouchers) {
      if (v.aad_id == null) continue;  // レガシー平文証憑 (E2EE 対象外)
      const aadId = BigInt(v.aad_id);
      if (v.encrypted_meta_blob && v.meta_iv) {
        metaRaw.push({
          key: { id: v.id }, blobB64: v.encrypted_meta_blob,
          ivB64: v.meta_iv, aad: buildAAD("vmeta", userId, aadId),
        });
      }
      const aadLog = buildAAD("valog", userId, aadId);
      for (const lg of v.logs || []) {
        if (lg.encrypted_detail_blob && lg.detail_iv) {
          logRaw.push({
            key: { id: lg.id }, blobB64: lg.encrypted_detail_blob,
            ivB64: lg.detail_iv, aad: aadLog,
          });
        }
      }
    }

    const meta = await rewrapRecordItems(client, metaRaw);
    const log = await rewrapRecordItems(client, logRaw);
    counts.vmeta += await _postRewrap(fetchImpl, "vmeta", meta.items);
    counts.valog += await _postRewrap(fetchImpl, "valog", log.items);
    counts.vmetaSkipped += meta.skipped;
    counts.valogSkipped += log.skipped;

    // 画像は 1 件ずつ (大きいので一括 POST にしない)。
    for (const v of vouchers) {
      if (v.aad_id == null || !v.has_image) continue;
      const sent = await rewrapVoucherImage({ client, userId, voucher: v, fetchImpl });
      if (sent) counts.vimg++;
      if (onProgress) onProgress();
    }

    if (vouchers.length === 0 || page * VOUCHER_PAGE >= total) break;
    page++;
  }
  return counts;
}


/**
 * 再ラップフロー本体。temp-MK 取得 → setRewrapKey → 全テーブル再ラップ →
 * clearRewrapKey → finalize。本物 MK は事前に解錠済 (status().hasKey) が前提。
 *
 * @param {Object} a
 * @param {Object} a.client    SharedCryptoClient (本物 MK 解錠済)
 * @param {number} a.userId
 * @param {Array<number>} a.years  je/jel/bcb を走査する年度
 * @param {Function} [a.fetchImpl]
 * @param {Function} [a.onProgress]  (done, totalSteps) を受け取る進捗コールバック
 * @returns {Promise<Object>} 再ラップ件数サマリ
 */
export async function runRewrapMigration({
  client, userId, years, fetchImpl, onProgress,
}) {
  const f = fetchImpl ?? globalThis.fetch;

  // 1) temp-MK を取得 (セッション限定)。active でなければ移行不要。
  const tm = await _getJson(f, "/api/v1/migration/temp-mk");
  if (!tm.active || !tm.temp_mk) {
    return { active: false };
  }

  // 2) 副鍵 (temp-MK) を Worker に decrypt 専用で設定。
  const tempMk = b64decode(tm.temp_mk);
  await client.setRewrapKey(tempMk);

  const summary = {
    active: true, je: 0, jel: 0, me: 0, bcb: 0,
    vmeta: 0, valog: 0, vimg: 0,
  };
  // 進捗の総ステップ = 年度数(2 種: je/bcb) + 医療費(1) + 証憑(1) + finalize(1)。
  const totalSteps = years.length * 2 + 3;
  let done = 0;
  const tick = () => { done++; if (onProgress) onProgress(done, totalSteps); };

  try {
    for (const year of years) {
      const j = await rewrapJournalsForYear({ client, userId, year, fetchImpl: f });
      summary.je += j.je;
      summary.jel += j.jel;
      tick();
      const b = await rewrapBalanceCacheForYear({
        client, userId, year, fetchImpl: f,
      });
      summary.bcb += b.bcb;
      tick();
    }

    const m = await rewrapMedicalExpenses({ client, userId, fetchImpl: f });
    summary.me += m.me;
    tick();

    const v = await rewrapVouchers({ client, userId, fetchImpl: f });
    summary.vmeta += v.vmeta;
    summary.valog += v.valog;
    summary.vimg += v.vimg;
    tick();

    // 3) 全再ラップ完了 → finalize で temp_mk を破棄 (真の E2EE 確立)。
    const fin = await _postJson(f, "/api/v1/migration/finalize", {});
    summary.finalized = !!fin.finalized;
    tick();
  } finally {
    // 副鍵は必ず破棄 (失敗時も temp-MK を Worker に残さない)。
    try { await client.clearRewrapKey(); } catch (_e) { /* ignore */ }
  }

  return summary;
}
