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

    // 確定済 period 0..closed_period (+ 15 まで確定なら 16 = 損益振替済)
    const toSync = [];
    for (let p = 0; p <= params.closed_period; p++) {
      if (!existingPeriods.has(p)) toSync.push(p);
    }
    if (params.closed_period >= 15 && !existingPeriods.has(16)) {
      toSync.push(16);
    }

    // 確定解除に伴い stale になった BCB を削除。closed_period < 15 のときは
    // closed_period+1 以降を削除 (16 含む)。closed_period >= 15 のときは
    // 16 (損益振替済の累計) を保持するため削除しない。
    let stalePruned = 0;
    if (params.closed_period < 15) {
      const staleFromPeriod = params.closed_period + 1;
      const hasStale = [...existingPeriods].some((p) => p >= staleFromPeriod);
      if (hasStale) {
        const res = await deleteBalanceCacheBlobs({
          year: params.year, fromPeriod: staleFromPeriod,
        });
        stalePruned = res.deleted || 0;
      }
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
