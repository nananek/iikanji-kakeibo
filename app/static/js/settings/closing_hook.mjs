// #338 item1: 決算月3 (period15) 確定ボタンのクライアント closing 生成フック。
//
// 旧実装ではサーバ (close_period → generate_closing_entries) が平文金額を SQL SUM
// して損益振替仕訳を生成していた。E2EE 化でサーバは MK を持たないため、確定ボタン
// 押下時にクライアントが全仕訳を復号・集計して closing 仕訳を暗号化生成し、専用
// エンドポイント (close-closing) へ送ってアトミックに period15 を確定する。
//
// MK ロック中は closing を計算できないため確定を中断し解錠を促す (BCB sync の
// silent skip と異なり、ここはユーザー操作の中断が正しい)。

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
    // フォールバック (showToast 未定義の最小環境)。
    // eslint-disable-next-line no-alert
    globalThis.alert(message);
  }
}


async function _onConfirm(btn) {
  const year = Number.parseInt(btn.dataset.year, 10);
  if (!Number.isInteger(year)) return;
  if (!globalThis.confirm(
    "決算月3を確定すると損益振替仕訳が生成されます。よろしいですか？",
  )) return;

  let accountsMeta;
  let userId;
  try {
    const metaEl = document.getElementById("closing-accounts-meta");
    const paramsEl = document.getElementById("bcb-sync-params");
    accountsMeta = JSON.parse(metaEl.textContent);
    userId = JSON.parse(paramsEl.textContent).user_id;
  } catch (e) {
    toast("確定に必要なデータを読み込めませんでした。", "danger");
    return;
  }
  if (typeof userId !== "number") {
    toast("確定に必要なデータを読み込めませんでした。", "danger");
    return;
  }

  const [{ SharedCryptoClient }, { buildAndPostClosingEntry }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/shared-client.js"),
    import(getStaticRoot() + "js/crypto/reports/closing.js"),
  ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  btn.disabled = true;
  try {
    const status = await client.status();
    if (!status.hasKey) {
      // MK ロック中は closing を計算できない。確定を中断し解錠を促す。
      toast(
        "暗号鍵がロックされています。設定 → 暗号鍵管理 で解除してから確定してください。",
        "danger",
      );
      btn.disabled = false;
      return;
    }
    await buildAndPostClosingEntry({ client, userId, year, accountsMeta });
    globalThis.location.reload();
  } catch (e) {
    toast(
      "決算月3の確定に失敗しました: " + ((e && e.message) || e),
      "danger",
    );
    btn.disabled = false;
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


function _bind() {
  const btn = document.getElementById("closing-confirm-btn");
  if (btn) {
    btn.addEventListener("click", () => { _onConfirm(btn); });
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bind);
  } else {
    _bind();
  }
}

export { _onConfirm };
