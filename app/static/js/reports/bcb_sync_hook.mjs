// Phase E3-E-4: 月次確定 UI からの BCB 自動 sync hook。
//
// `#bcb-sync-params` (type=application/json) に
//   {year, closed_period, user_id, is_audit_proxy}
// を埋め込んだページで読み込まれると、確定済 period のうち BCB がまだない
// ものを生成して PUT する。MK ロック時 / 監査代理閲覧時 / 確定なし時は skip。
//
// 既存 BCB は GET で確認するので、毎回ページロードしても無駄な PUT は走らない。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


/**
 * sync すべき period と削除すべき stale period の起点を計算する純粋関数。
 *
 * - 確定済 (0..closedPeriod) のうち existing にないものを toSync に列挙
 * - closedPeriod >= 15 で 16 (損益振替済の累計) が existing にないなら追加
 * - closedPeriod < 15 のとき closedPeriod+1 以降に existing があれば
 *   staleFromPeriod に closedPeriod+1 を設定 (DELETE 対象)
 * - closedPeriod >= 15 のときは 16 を保持するため stale 削除しない
 *
 * @param {Set<number>} existingPeriods
 * @param {number} closedPeriod  -1=未確定, 0..15
 * @returns {{ toSync: number[], staleFromPeriod: (number|null) }}
 */
export function planBcbSync(existingPeriods, closedPeriod) {
  const toSync = [];
  if (closedPeriod >= 0) {
    for (let p = 0; p <= closedPeriod; p++) {
      if (!existingPeriods.has(p)) toSync.push(p);
    }
    if (closedPeriod >= 15 && !existingPeriods.has(16)) {
      toSync.push(16);
    }
  }

  let staleFromPeriod = null;
  if (closedPeriod < 15) {
    const candidate = (closedPeriod < 0) ? 0 : closedPeriod + 1;
    if ([...existingPeriods].some((p) => p >= candidate)) {
      staleFromPeriod = candidate;
    }
  }

  return { toSync, staleFromPeriod };
}


async function _run() {
  const paramsEl = document.getElementById("bcb-sync-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("bcb_sync_hook: failed to parse params", e);
    return;
  }
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;
  if (typeof params.year !== "number") return;
  if (typeof params.closed_period !== "number" || params.closed_period < 0) return;

  const [
    { SharedCryptoClient },
    bcbMod,
    { syncBalanceCacheForPeriods },
  ] = await Promise.all([
    import(getStaticRoot() + "js/crypto/shared-client.js"),
    import(getStaticRoot() + "js/crypto/balance_cache_blobs_client.js"),
    import(getStaticRoot() + "js/crypto/bcb_sync.js"),
  ]);
  const { fetchBalanceCacheBlobs, deleteBalanceCacheBlobs } = bcbMod;

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("bcb_sync_hook: MK locked, skipping");
      return;
    }
    const existing = await fetchBalanceCacheBlobs({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const existingPeriods = new Set(
      Object.keys(existing).map((k) => Number.parseInt(k, 10)),
    );

    // どの period を sync / DELETE するかは planBcbSync で決定 (純粋関数)
    const { toSync, staleFromPeriod } = planBcbSync(
      existingPeriods, params.closed_period,
    );

    let stalePruned = 0;
    if (staleFromPeriod !== null) {
      const res = await deleteBalanceCacheBlobs({
        year: params.year, fromPeriod: staleFromPeriod,
      });
      stalePruned = res.deleted || 0;
    }

    if (toSync.length === 0 && stalePruned === 0) {
      console.info("bcb_sync_hook: all periods up-to-date");
      return;
    }
    if (toSync.length > 0) {
      await syncBalanceCacheForPeriods({
        client, userId: params.user_id, year: params.year, periods: toSync,
      });
    }
    console.info(
      `%c✓ bcb_sync_hook: synced=${toSync.length}, pruned=${stalePruned} (year=${params.year})`,
      "color: green; font-weight: bold",
    );
  } catch (e) {
    console.warn("bcb_sync_hook: error", e);
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
