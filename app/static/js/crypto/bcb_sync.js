// Phase E3-E-3: balance_cache_blobs の生成 wrapper。
//
// 月次確定後にクライアントから呼び出して、指定 (year, period) の暗号化済
// balance cache blob を生成 → PUT する。
//
// サーバ側 compute_balance_cache (= balance_caches 平文) と並走する形で
// blob を生成し、E3-F で平文側を撤去する流れ。
//
// 依存:
//   - fetchJournalsForYear (journals_client.js)
//   - computeBalanceCache (reports/balance_cache.js)
//   - saveBalanceCacheBlob (balance_cache_blobs_client.js)

import { fetchJournalsForYear } from "./journals_client.js";
import { saveBalanceCacheBlob } from "./balance_cache_blobs_client.js";
import { computeBalanceCache } from "./reports/balance_cache.js";


/**
 * 指定 (year, period) の BCB を生成して PUT する。
 *
 * @param {Object} args
 * @param {Object} args.client            SharedCryptoClient (encrypt + decrypt)
 * @param {number|bigint} args.userId
 * @param {number} args.year
 * @param {number} args.period            0..16
 * @param {Function} [args.fetchImpl=globalThis.fetch]
 * @param {Function} [args.fetchJournalsImpl]      テスト DI
 * @param {Function} [args.saveBcbImpl]            テスト DI
 * @returns {Promise<{ok: boolean, updated_at: string}>}
 */
export async function syncBalanceCacheForPeriod({
  client, userId, year, period, fetchImpl,
  fetchJournalsImpl = fetchJournalsForYear,
  saveBcbImpl = saveBalanceCacheBlob,
}) {
  if (!Number.isInteger(period) || period < 0 || period > 16) {
    throw new TypeError("period must be an integer 0..16");
  }
  const entries = await fetchJournalsImpl({
    client, userId, fiscalYear: year, fetchImpl,
  });
  const balances = computeBalanceCache(entries, { period });
  return await saveBcbImpl({
    client, userId, year, period, balances, fetchImpl,
  });
}


/**
 * 複数 period をまとめて sync (例: period=3 まで確定済み → 0,1,2,3 を生成)。
 * 早期失敗 (1 つでも fail なら throw、それ以前の period は既に PUT 済)。
 *
 * @param {Object} args
 * @param {Object} args.client
 * @param {number|bigint} args.userId
 * @param {number} args.year
 * @param {Array<number>} args.periods    例: [0, 1, 2, 3]
 * @param {Function} [args.fetchImpl]
 * @param {Function} [args.fetchJournalsImpl]
 * @param {Function} [args.saveBcbImpl]
 * @returns {Promise<Array<{period, updated_at}>>}
 */
export async function syncBalanceCacheForPeriods({
  client, userId, year, periods, fetchImpl,
  fetchJournalsImpl = fetchJournalsForYear,
  saveBcbImpl = saveBalanceCacheBlob,
}) {
  if (!Array.isArray(periods) || periods.length === 0) {
    throw new TypeError("periods must be a non-empty array");
  }
  // fetchJournals は 1 度だけ呼んで全 period で使い回す (N+1 回避)
  const entries = await fetchJournalsImpl({
    client, userId, fiscalYear: year, fetchImpl,
  });
  const results = [];
  for (const period of periods) {
    if (!Number.isInteger(period) || period < 0 || period > 16) {
      throw new TypeError(`period must be 0..16 (got ${period})`);
    }
    const balances = computeBalanceCache(entries, { period });
    const res = await saveBcbImpl({
      client, userId, year, period, balances, fetchImpl,
    });
    results.push({ period, updated_at: res.updated_at });
  }
  return results;
}
