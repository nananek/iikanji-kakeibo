// E7 (#114) 再ラップフローのダッシュボードバナー連携 (再ラップフロー PR-3)。
//
// ダッシュボード上部の「E2EE 移行を完了する」バナー (#migration-rewrap-banner) の
// ボタン click で、サーバが #migration-rewrap-params に供給した {user_id, years} を
// もとに runRewrapMigration を実行する。本物 MK 未解錠なら中断し解錠を促す
// (closing 移行と同方針)。完了で reload してバナーを消す。

function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function toast(message, type) {
  if (typeof globalThis.showToast === "function") {
    globalThis.showToast(message, type);
  } else {
    globalThis.alert(message);
  }
}


function _setProgress(el, done, total) {
  if (!el) return;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  el.style.width = pct + "%";
  el.setAttribute("aria-valuenow", String(pct));
  el.textContent = pct + "%";
}


async function _onMigrate(btn) {
  let userId;
  let years;
  try {
    const params = JSON.parse(
      document.getElementById("migration-rewrap-params").textContent,
    );
    userId = params.user_id;
    years = params.years;
  } catch (_e) {
    toast("移行に必要なデータを読み込めませんでした。", "danger");
    return;
  }
  if (typeof userId !== "number" || !Array.isArray(years)) {
    toast("移行に必要なデータが不正です。", "danger");
    return;
  }

  const [{ SharedCryptoClient }, { runRewrapMigration }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/shared-client.js"),
    import(getStaticRoot() + "js/migration/rewrap_flow.js"),
  ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  const progressWrap = document.getElementById("migration-rewrap-progress");
  const progressBar = document.getElementById("migration-rewrap-bar");
  btn.disabled = true;
  try {
    const status = await client.status();
    if (!status.hasKey) {
      toast(
        "暗号鍵がロックされています。設定 → 暗号鍵管理 で解除してから移行してください。",
        "danger",
      );
      btn.disabled = false;
      return;
    }

    if (progressWrap) progressWrap.classList.remove("d-none");
    const result = await runRewrapMigration({
      client, userId, years,
      onProgress: (done, total) => _setProgress(progressBar, done, total),
    });

    if (result.active === false) {
      // 既に移行済 (temp_mk なし) → バナーを消すだけ。
      toast("E2EE 移行は既に完了しています。", "info");
    } else {
      toast("E2EE 移行が完了しました。", "success");
    }
    globalThis.location.reload();
  } catch (e) {
    // showToast は innerHTML を使うため、サーバ応答由来の文字列 (e.message には
    // _getJson/_postJson 経由でサーバの error フィールドが含まれうる) をそのまま
    // 渡すと XSS 経路になる。toast には固定文言のみ渡し、詳細は console に出す。
    console.error("E2EE 再ラップ移行に失敗:", e);
    toast(
      "E2EE 移行に失敗しました。時間をおいて再実行してください。",
      "danger",
    );
    btn.disabled = false;
    if (progressWrap) progressWrap.classList.add("d-none");
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


function _bind() {
  const btn = document.getElementById("migration-rewrap-btn");
  if (btn) {
    btn.addEventListener("click", () => { _onMigrate(btn); });
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bind);
  } else {
    _bind();
  }
}

export { _onMigrate };
