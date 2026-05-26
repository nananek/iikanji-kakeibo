// Phase v5 BU-1: 全データバックアップ JSON download UI。
//
// MK 復号 → /api/v1/backup/export → decryptBackup → 平文 JSON ファイル保存。
// 失敗系は backup-status に表示する。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("backup-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " small";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("backup-status");
  if (el) el.classList.add("d-none");
}


function _filename() {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  return `iikanji-backup-${ts}.json`;
}


function _downloadJSON(obj, filename) {
  const blob = new Blob(
    [JSON.stringify(obj, null, 2)],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // revokeObjectURL は click ハンドラが終わった直後だと Firefox で
  // ダウンロードがキャンセルされる事があるので少し遅延させる
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}


export async function runBackupExport() {
  _clearStatus();
  const btn = document.getElementById("backup-export-btn");
  if (btn) btn.disabled = true;

  let client;
  try {
    const [
      { SharedCryptoClient },
      { decryptBackup },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/backup_export_client.js"),
    ]);

    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      _setStatus(
        "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
        "warning",
      );
      return;
    }

    _setStatus("サーバから暗号文付きデータを取得しています…", "info");
    // /api/v1/* は CSRF 免除エンドポイント、かつ GET なので CSRFToken は不要。
    const resp = await fetch("/api/v1/backup/export", {
      method: "GET",
      credentials: "same-origin",
    });
    if (!resp.ok) {
      const text = await resp.text();
      _setStatus(
        `バックアップ取得に失敗しました (HTTP ${resp.status}): ${text.slice(0, 200)}`,
        "danger",
      );
      return;
    }
    const backup = await resp.json();

    _setStatus("ローカルで復号しています…", "info");
    const decrypted = await decryptBackup(client, backup);

    // 復号失敗 / 画像取得失敗があれば警告表示
    let decryptFailures = 0;
    for (const tbl of [
      "journal_entries", "journal_entry_lines",
      "medical_expenses", "balance_cache_blobs",
    ]) {
      for (const r of decrypted.data[tbl] || []) {
        if (r._decryptError) decryptFailures += 1;
      }
    }
    let imageFailures = 0;
    for (const v of decrypted.data.vouchers || []) {
      if (v._imageError) imageFailures += 1;
    }
    _downloadJSON(decrypted, _filename());
    const warnings = [];
    if (decryptFailures > 0) {
      warnings.push(
        `${decryptFailures} 件の暗号文は復号できなかったため _decryptError として記録`,
      );
    }
    if (imageFailures > 0) {
      warnings.push(
        `${imageFailures} 件の証憑画像は取得できなかったため _imageError として記録`,
      );
    }
    if (warnings.length > 0) {
      _setStatus("ダウンロード完了。" + warnings.join("、"), "warning");
    } else {
      _setStatus("ダウンロード完了。", "success");
    }
  } catch (e) {
    _setStatus(
      "バックアップ処理に失敗しました: " + (e.message || e),
      "danger",
    );
  } finally {
    if (btn) btn.disabled = false;
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}
