// BCB 自動 sync hook: #bcb-sync-params JSON を起点に未生成の period を PUT、
// reopen で stale になった BCB を DELETE する (browser-only)。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


// closedPeriod = -1 (未確定) でも stale BCB の削除は必要なので、planBcbSync は
// 全期間 reopen 状態でも staleFromPeriod=0 を返す (caller は早期 return 不要)。
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
    let hasStale = false;
    for (const p of existingPeriods) {
      if (p >= candidate) { hasStale = true; break; }
    }
    if (hasStale) staleFromPeriod = candidate;
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
  // closed_period = -1 (全期間 reopen 後) でも stale 削除が走るよう、< 0 で
  // 早期 return しない。planBcbSync が toSync=[] かつ stale 無しを判断する。
  if (typeof params.closed_period !== "number") return;

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
