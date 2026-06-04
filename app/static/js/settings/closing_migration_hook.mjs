// #338 旧 closing 移行: 旧形式 (item1 以前のサーバ生成・空 encrypted_blob) の損益振替を
// クライアントで再計算・暗号化し reencrypt-closing で置換するフック。
//
// fiscal ページの「すべて再暗号化」ボタン click で、対象年度 (server が
// #closing-migration-params で供給) を順に処理する。MK ロック中は中断し解錠を促す
// (closing 確定と同方針)。確定済み年度はロックされ revenue/expense を編集できないため
// 再計算 closing は元と同額。

function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function toast(message, type) {
  if (typeof globalThis.showToast === "function") {
    globalThis.showToast(message, type);
  } else {
    globalThis.alert(message);
  }
}


async function _onMigrate(btn) {
  let years;
  let userId;
  let accountsMeta;
  try {
    const params = JSON.parse(
      document.getElementById("closing-migration-params").textContent,
    );
    years = params.years;
    userId = params.user_id;
    accountsMeta = JSON.parse(
      document.getElementById("closing-accounts-meta").textContent,
    );
  } catch (e) {
    toast("移行に必要なデータを読み込めませんでした。", "danger");
    return;
  }
  if (!Array.isArray(years) || years.length === 0 || typeof userId !== "number") {
    return;
  }

  const [{ SharedCryptoClient }, { reencryptOldClosings }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/shared-client.js"),
    import(getStaticRoot() + "js/crypto/reports/closing.js"),
  ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  btn.disabled = true;
  try {
    const status = await client.status();
    if (!status.hasKey) {
      toast(
        "暗号鍵がロックされています。設定 → 暗号鍵管理 で解除してから再暗号化してください。",
        "danger",
      );
      btn.disabled = false;
      return;
    }
    await reencryptOldClosings({ client, userId, years, accountsMeta });
    globalThis.location.reload();
  } catch (e) {
    toast(
      "損益振替の再暗号化に失敗しました: " + ((e && e.message) || e),
      "danger",
    );
    btn.disabled = false;
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


function _bind() {
  const btn = document.getElementById("closing-migrate-btn");
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
