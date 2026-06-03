// E6 (#113) 全データエクスポート UI オーケストレータ。
//
// PR-1: MK 復号 → CSV + 証憑画像 + README + backup.json を fflate で zip 化 →
//   ブラウザで直接ダウンロード。
// PR-2: 同じ zip を encryptBackupArchive (パスフレーズ) で暗号化し、サーバに
//   一時保存 → メールリンクで非同期受け取り。保存済みジョブ一覧から「復号して
//   ダウンロード」(blob 取得 → decryptBackupArchive → 平文 zip 保存)。
//
// サーバ往復は /api/v1/backup/export (取得) と /api/v1/export/jobs (保存/一覧/DL)。
// DOM 依存の glue なので CI の crypto カバレッジ gate 対象外。純ロジックは
// export/csv.js (PR-1 でテスト済) と crypto/backup_archive.js (既存テスト)。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


const MIME_EXT = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/gif": "gif",
  "image/webp": "webp",
};

// 大容量ガード閾値 (§15.4)。超えると client-py 推奨ダイアログを出す。
const MAX_VOUCHERS = 200;
const MAX_TOTAL_BYTES = 200 * 1024 * 1024;


function _setStatus(msg, type = "info") {
  const el = document.getElementById("export-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " small";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("export-status");
  if (el) el.classList.add("d-none");
}


function _timestamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}


function _downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Firefox では click 直後の revoke でキャンセルされるので遅延
  setTimeout(() => URL.revokeObjectURL(url), 1000);
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


async function _fetchAndDecrypt(client, decryptBackup) {
  _setStatus("サーバから暗号文付きデータを取得しています…", "info");
  const resp = await fetch("/api/v1/backup/export", {
    method: "GET",
    credentials: "same-origin",
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(
      `データ取得に失敗しました (HTTP ${resp.status}): ${text.slice(0, 200)}`,
    );
  }
  const backup = await resp.json();
  _setStatus("ローカルで復号しています…", "info");
  const decrypted = await decryptBackup(client, backup);
  return { backup, decrypted };
}


function _confirmLargeExport(vouchers) {
  const totalBytes = vouchers.reduce((s, v) => s + (v.file_size || 0), 0);
  if (vouchers.length <= MAX_VOUCHERS && totalBytes <= MAX_TOTAL_BYTES) {
    return true;
  }
  const mb = Math.round(totalBytes / (1024 * 1024));
  return globalThis.confirm(
    `証憑が ${vouchers.length} 件 / 約 ${mb} MB あります。\n` +
    "ブラウザのメモリ上限に近づき失敗する場合があります。\n" +
    "大規模データは client-py の `iikanji export` を推奨します。\n\n" +
    "このまま続行しますか?",
  );
}


function _countDecryptFailures(decrypted) {
  let n = 0;
  for (const tbl of ["journal_entries", "journal_entry_lines", "medical_expenses"]) {
    for (const r of decrypted.data[tbl] || []) {
      if (r._decryptError) n += 1;
    }
  }
  return n;
}


async function _buildVoucherImages(client, userId, vouchers, deps, files) {
  const { b64decode, decryptVoucherBlob, sniffImageMime } = deps;
  const imageNames = new Map();
  let imageFail = 0;
  for (const v of vouchers) {
    if (v._imageError || !v.image_data) {
      imageFail += 1;
      continue;
    }
    try {
      const enc = b64decode(v.image_data);
      // E4 (#111) 以降の証憑は aad_id ありで暗号化済。aad_id なしは旧平文。
      const plain = v.aad_id
        ? await decryptVoucherBlob({
            client, userId, aadId: v.aad_id, blob: enc,
          })
        : enc;
      const ext = MIME_EXT[sniffImageMime(plain)] || "bin";
      const base = `voucher_${v.id}.${ext}`;
      files["vouchers/" + base] = [plain, { level: 0 }];
      imageNames.set(v.id, base);
    } catch (_e) {
      imageFail += 1;
    }
  }
  return { imageNames, imageFail };
}


function _readme(exportedAt, decryptFailures, imageFail) {
  return (
    "いいかんじ™家計簿 データエクスポート\n" +
    `生成日時: ${exportedAt || "(不明)"}\n` +
    "\n" +
    "【内容】\n" +
    "- journal.csv   : 仕訳帳 (明細 1 行 = 1 レコード)\n" +
    "- accounts.csv  : 勘定科目マスタ\n" +
    "- medical.csv   : 医療費\n" +
    "- vouchers.csv  : 証憑メタデータ\n" +
    "- vouchers/     : 証憑画像ファイル\n" +
    "- backup.json   : 機械可読バックアップ (暗号文のまま)。\n" +
    "                  設定 → 全データリストア で再取り込みできます。\n" +
    "\n" +
    "【注意】\n" +
    "- CSV は UTF-8 (BOM 付き) です。Excel でそのまま開けます。\n" +
    "- backup.json はサーバが保持する暗号文をそのまま含みます。復号には\n" +
    "  本人のマスター鍵 (MK) が必要です。\n" +
    '- 復号できなかったレコードは CSV 上 "(復号失敗)" と表示されます。\n' +
    "\n" +
    `復号失敗: ${decryptFailures} 件\n` +
    `画像取得失敗: ${imageFail} 件\n`
  );
}


async function _loadDeps() {
  const [
    { decryptBackup },
    { b64decode },
    { decryptVoucherBlob, sniffImageMime },
    { zipSync },
    csv,
  ] = await Promise.all([
    import(getStaticRoot() + "js/crypto/backup_export_client.js"),
    import(getStaticRoot() + "js/crypto/b64.js"),
    import(getStaticRoot() + "js/crypto/voucher_download.js"),
    import(getStaticRoot() + "js/vendor/fflate.module.js"),
    import(getStaticRoot() + "js/export/csv.js"),
  ]);
  return { decryptBackup, b64decode, decryptVoucherBlob, sniffImageMime, zipSync, csv };
}


/**
 * 全データを取得・復号し、CSV/画像/backup.json/README を 1 つの zip にする。
 * 大容量ガードでユーザーがキャンセルした場合は null を返す。
 *
 * @returns {Promise<{zipped: Uint8Array, decryptFailures: number,
 *                     imageFail: number} | null>}
 */
async function _buildZip(client, deps) {
  const { backup, decrypted } = await _fetchAndDecrypt(client, deps.decryptBackup);

  const vouchers = decrypted.data.vouchers || [];
  if (!_confirmLargeExport(vouchers)) return null;

  _setStatus("ファイルを生成しています…", "info");
  const enc = new TextEncoder();
  const textFile = (s) => enc.encode("﻿" + s);  // UTF-8 BOM
  const files = {};

  const { imageNames, imageFail } = await _buildVoucherImages(
    client, decrypted.user_id, vouchers, deps, files,
  );

  files["journal.csv"] = textFile(deps.csv.buildJournalCsv(decrypted.data));
  files["accounts.csv"] = textFile(deps.csv.buildAccountsCsv(decrypted.data));
  files["medical.csv"] = textFile(deps.csv.buildMedicalCsv(decrypted.data));
  files["vouchers.csv"] = textFile(
    deps.csv.buildVouchersCsv(decrypted.data, imageNames),
  );
  files["backup.json"] = enc.encode(JSON.stringify(backup));

  const decryptFailures = _countDecryptFailures(decrypted);
  files["README.txt"] = textFile(
    _readme(backup.exported_at, decryptFailures, imageFail),
  );

  _setStatus("zip を圧縮しています…", "info");
  const zipped = deps.zipSync(files, { level: 6 });
  return { zipped, decryptFailures, imageFail };
}


function _warningTail(decryptFailures, imageFail) {
  const w = [];
  if (decryptFailures > 0) w.push(`${decryptFailures} 件の復号失敗`);
  if (imageFail > 0) w.push(`${imageFail} 件の画像取得失敗`);
  return w;
}


// --- PR-1: 直接ダウンロード ---

export async function runExport() {
  _clearStatus();
  const btn = document.getElementById("export-btn");
  if (btn) btn.disabled = true;

  let client;
  try {
    const deps = await _loadDeps();
    client = await _setupCryptoClient();
    if (!client) return;

    const built = await _buildZip(client, deps);
    if (!built) {
      _setStatus("エクスポートをキャンセルしました。", "info");
      return;
    }
    _downloadBlob(
      new Blob([built.zipped], { type: "application/zip" }),
      `iikanji-export-${_timestamp()}.zip`,
    );
    const w = _warningTail(built.decryptFailures, built.imageFail);
    _setStatus(
      "ダウンロード完了。" + (w.length ? w.join("、") + "。" : ""),
      w.length ? "warning" : "success",
    );
  } catch (e) {
    _setStatus("エクスポートに失敗しました: " + (e.message || e), "danger");
  } finally {
    if (btn) btn.disabled = false;
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


// --- PR-2: サーバ保存 + メール配信 ---

function _readServerPassphrase() {
  const el = document.getElementById("export-passphrase");
  return el ? (el.value || "") : "";
}


export async function runServerExport() {
  _clearStatus();
  const passphrase = _readServerPassphrase();
  if (!passphrase || passphrase.length < 8) {
    _setStatus(
      "サーバ保存にはパスフレーズ (8 文字以上) が必要です。",
      "warning",
    );
    return;
  }

  const btn = document.getElementById("export-server-btn");
  if (btn) btn.disabled = true;

  let client;
  try {
    const [deps, { encryptBackupArchive }, { loadHashWasm }] = await Promise.all([
      _loadDeps(),
      import(getStaticRoot() + "js/crypto/backup_archive.js"),
      import(getStaticRoot() + "js/crypto/hash_wasm_loader.js"),
    ]);
    await loadHashWasm();  // Argon2id 実装をロード

    client = await _setupCryptoClient();
    if (!client) return;

    const built = await _buildZip(client, deps);
    if (!built) {
      _setStatus("エクスポートをキャンセルしました。", "info");
      return;
    }

    _setStatus("パスフレーズで暗号化しています… (数秒かかります)", "info");
    const archive = await encryptBackupArchive(built.zipped, passphrase);

    _setStatus("サーバにアップロードしています…", "info");
    const resp = await fetch("/api/v1/export/jobs", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/octet-stream" },
      body: archive,
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`アップロード失敗 (HTTP ${resp.status}): ${text.slice(0, 200)}`);
    }

    const w = _warningTail(built.decryptFailures, built.imageFail);
    _setStatus(
      "サーバに保存しました。完了メールの案内に従ってダウンロードしてください。" +
      (w.length ? " " + w.join("、") + "。" : ""),
      w.length ? "warning" : "success",
    );
    const pp = document.getElementById("export-passphrase");
    if (pp) pp.value = "";
    await loadExportJobs();
  } catch (e) {
    _setStatus("サーバ保存に失敗しました: " + (e.message || e), "danger");
  } finally {
    if (btn) btn.disabled = false;
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


function _fmtBytes(n) {
  if (!n) return "0 B";
  const mb = n / (1024 * 1024);
  if (mb >= 1) return mb.toFixed(1) + " MB";
  return Math.round(n / 1024) + " KB";
}


function _fmtDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch (_e) {
    return iso;
  }
}


const _STATUS_LABEL = {
  ready: "保存済み",
  expired: "期限切れ",
  failed: "失敗",
};


export async function loadExportJobs() {
  const container = document.getElementById("export-jobs");
  if (!container) return;
  container.textContent = "読み込み中…";
  try {
    const resp = await fetch("/api/v1/export/jobs", {
      method: "GET",
      credentials: "same-origin",
    });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    _renderJobs(container, data.jobs || []);
  } catch (e) {
    container.textContent = "一覧の取得に失敗しました: " + (e.message || e);
  }
}


// DOM API でのみ描画する (innerHTML にデータを流さない = XSS 防止)。
function _renderJobs(container, jobs) {
  container.textContent = "";
  if (jobs.length === 0) {
    const p = document.createElement("p");
    p.className = "text-muted small mb-0";
    p.textContent = "保存済みのエクスポートはありません。";
    container.appendChild(p);
    return;
  }
  const ul = document.createElement("ul");
  ul.className = "list-group";
  for (const job of jobs) {
    const li = document.createElement("li");
    li.className =
      "list-group-item d-flex justify-content-between align-items-center";

    const left = document.createElement("div");
    left.className = "small";
    const label = _STATUS_LABEL[job.status] || job.status;
    const meta = document.createElement("div");
    meta.textContent =
      `${label} · ${_fmtBytes(job.file_size)} · 期限 ${_fmtDate(job.expires_at)}`;
    const sub = document.createElement("div");
    sub.className = "text-muted";
    sub.textContent =
      `作成 ${_fmtDate(job.created_at)} · DL ${job.download_count} 回`;
    left.appendChild(meta);
    left.appendChild(sub);

    const btn = document.createElement("button");
    btn.className = "btn btn-sm btn-outline-primary";
    btn.textContent = "復号してダウンロード";
    if (job.status !== "ready") {
      btn.disabled = true;
    } else {
      btn.addEventListener("click", () => downloadExportJob(job.id));
    }

    li.appendChild(left);
    li.appendChild(btn);
    ul.appendChild(li);
  }
  container.appendChild(ul);
}


export async function downloadExportJob(jobId) {
  const passphrase = globalThis.prompt(
    "エクスポート時に設定したパスフレーズを入力してください:",
  );
  if (!passphrase) return;

  _clearStatus();
  _setStatus("ダウンロードしています…", "info");
  try {
    const { decryptBackupArchive } = await import(
      getStaticRoot() + "js/crypto/backup_archive.js"
    );
    const { loadHashWasm } = await import(
      getStaticRoot() + "js/crypto/hash_wasm_loader.js"
    );
    await loadHashWasm();

    const resp = await fetch(`/api/v1/export/jobs/${jobId}/download`, {
      method: "GET",
      credentials: "same-origin",
    });
    if (resp.status === 410) {
      _setStatus("このエクスポートは有効期限切れ、または回数上限です。", "warning");
      await loadExportJobs();
      return;
    }
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status);
    }
    const archive = new Uint8Array(await resp.arrayBuffer());

    _setStatus("パスフレーズで復号しています…", "info");
    const zipBytes = await decryptBackupArchive(archive, passphrase);
    _downloadBlob(
      new Blob([zipBytes], { type: "application/zip" }),
      `iikanji-export-${jobId}.zip`,
    );
    _setStatus("ダウンロード完了。", "success");
    await loadExportJobs();
  } catch (e) {
    _setStatus(
      "復号に失敗しました (パスフレーズ誤り、または破損): " + (e.message || e),
      "danger",
    );
  }
}
