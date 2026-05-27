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


function _filename(ext = "json") {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  return `iikanji-backup-${ts}.${ext}`;
}


function _downloadBlob(blob, filename) {
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


function _downloadJSON(obj, filename) {
  _downloadBlob(
    new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" }),
    filename,
  );
}


function _readPassphrase() {
  const el = document.getElementById("backup-passphrase");
  if (!el) return "";
  return el.value || "";
}


function _readFormat() {
  const el = document.querySelector('input[name="backup-format"]:checked');
  return el ? el.value : "json";
}


export async function runBackupExport() {
  const format = _readFormat();
  if (format === "ikbackup") {
    const pass = _readPassphrase();
    if (!pass || pass.length < 8) {
      _setStatus(
        "暗号化アーカイブ形式にはパスフレーズ (8 文字以上) が必要です。",
        "warning",
      );
      return;
    }
    return _runEncryptedExport(pass);
  }
  return _runPlainExport();
}


function _countWarnings(decrypted) {
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
  for (const tbl of ["vouchers", "ai_drafts"]) {
    for (const v of decrypted.data[tbl] || []) {
      if (v._imageError) imageFailures += 1;
    }
  }
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
  return warnings;
}


async function _fetchAndDecrypt(client, decryptBackup) {
  _setStatus("サーバから暗号文付きデータを取得しています…", "info");
  // /api/v1/* は CSRF 免除エンドポイント、かつ GET なので CSRFToken は不要。
  const resp = await fetch("/api/v1/backup/export", {
    method: "GET",
    credentials: "same-origin",
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(
      `バックアップ取得に失敗しました (HTTP ${resp.status}): ${text.slice(0, 200)}`,
    );
  }
  const backup = await resp.json();
  _setStatus("ローカルで復号しています…", "info");
  return decryptBackup(client, backup);
}


async function _setupCryptoClient() {
  const { SharedCryptoClient } = await import(
    getStaticRoot() + "js/crypto/shared-client.js"
  );
  const client = new SharedCryptoClient(getSharedWorkerUrl());
  const status = await client.status();
  if (!status.hasKey) {
    client.close();
    _setStatus(
      "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
      "warning",
    );
    return null;
  }
  return client;
}


async function _runPlainExport() {
  _clearStatus();
  const btn = document.getElementById("backup-export-btn");
  if (btn) btn.disabled = true;

  let client;
  try {
    const { decryptBackup } = await import(
      getStaticRoot() + "js/crypto/backup_export_client.js"
    );
    client = await _setupCryptoClient();
    if (!client) return;

    const decrypted = await _fetchAndDecrypt(client, decryptBackup);
    _downloadJSON(decrypted, _filename("json"));
    const warnings = _countWarnings(decrypted);
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


async function _runEncryptedExport(passphrase) {
  _clearStatus();
  const btn = document.getElementById("backup-export-btn");
  if (btn) btn.disabled = true;

  let client;
  try {
    const [
      { decryptBackup },
      { encryptBackupArchive },
      { loadHashWasm },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/backup_export_client.js"),
      import(getStaticRoot() + "js/crypto/backup_archive.js"),
      import(getStaticRoot() + "js/crypto/hash_wasm_loader.js"),
    ]);
    // Argon2id 実装 (hash-wasm) を同 origin から動的ロード
    await loadHashWasm();
    client = await _setupCryptoClient();
    if (!client) return;

    const decrypted = await _fetchAndDecrypt(client, decryptBackup);

    _setStatus(
      "パスフレーズから鍵を派生し暗号化アーカイブを生成中… (数秒かかります)",
      "info",
    );
    const plaintextBytes = new TextEncoder().encode(
      JSON.stringify(decrypted),
    );
    const archive = await encryptBackupArchive(plaintextBytes, passphrase);
    _downloadBlob(
      new Blob([archive], { type: "application/octet-stream" }),
      _filename("ikbackup"),
    );

    const warnings = _countWarnings(decrypted);
    const tail = warnings.length > 0 ? " " + warnings.join("、") : "";
    _setStatus(
      "暗号化アーカイブのダウンロード完了。" +
      "復元には同じパスフレーズが必要です。" + tail,
      warnings.length > 0 ? "warning" : "success",
    );
  } catch (e) {
    _setStatus(
      "暗号化アーカイブ作成に失敗しました: " + (e.message || e),
      "danger",
    );
  } finally {
    if (btn) btn.disabled = false;
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}
