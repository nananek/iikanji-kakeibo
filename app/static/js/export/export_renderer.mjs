// E6 (#113) 全データエクスポート PR-1: 直接ダウンロード UI オーケストレータ。
//
// 流れ: MK 確認 → GET /api/v1/backup/export → decryptBackup で復号 →
//   人間可読 CSV + 証憑画像 (復号済) + README.txt + 機械可読 backup.json を
//   fflate で zip 化 → Blob URL で直接ダウンロード。
//
// サーバ往復は既存の /api/v1/backup/export のみ (新規 API なし)。DOM 依存の
// glue なので CI の crypto カバレッジ gate 対象外。純ロジックは export/csv.js。


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

// 大容量ガード閾値 (§15.4)。これを超えるとブラウザのメモリ上限に近づくため
// client-py の `iikanji export` を推奨する確認ダイアログを出す。
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
  // Firefox では click 直後の revoke でダウンロードがキャンセルされるので遅延
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
  // /api/v1/* は CSRF 免除、GET なので CSRFToken 不要。
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


export async function runExport() {
  _clearStatus();
  const btn = document.getElementById("export-btn");
  if (btn) btn.disabled = true;

  let client;
  try {
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

    client = await _setupCryptoClient();
    if (!client) return;

    const { backup, decrypted } = await _fetchAndDecrypt(client, decryptBackup);

    const vouchers = decrypted.data.vouchers || [];
    if (!_confirmLargeExport(vouchers)) {
      _setStatus("エクスポートをキャンセルしました。", "info");
      return;
    }

    _setStatus("ファイルを生成しています…", "info");
    const enc = new TextEncoder();
    const textFile = (s) => enc.encode("﻿" + s);  // UTF-8 BOM
    const files = {};

    // 証憑画像 (復号して平文バイトで格納) → ファイル名 Map を CSV に反映
    const { imageNames, imageFail } = await _buildVoucherImages(
      client, decrypted.user_id, vouchers,
      { b64decode, decryptVoucherBlob, sniffImageMime }, files,
    );

    files["journal.csv"] = textFile(csv.buildJournalCsv(decrypted.data));
    files["accounts.csv"] = textFile(csv.buildAccountsCsv(decrypted.data));
    files["medical.csv"] = textFile(csv.buildMedicalCsv(decrypted.data));
    files["vouchers.csv"] = textFile(
      csv.buildVouchersCsv(decrypted.data, imageNames),
    );

    // backup.json は raw レスポンス (暗号文のまま) を格納 → 既存 restore で再取込可
    files["backup.json"] = enc.encode(JSON.stringify(backup));

    const decryptFailures = _countDecryptFailures(decrypted);
    files["README.txt"] = textFile(
      _readme(backup.exported_at, decryptFailures, imageFail),
    );

    _setStatus("zip を圧縮しています…", "info");
    const zipped = zipSync(files, { level: 6 });
    _downloadBlob(
      new Blob([zipped], { type: "application/zip" }),
      `iikanji-export-${_timestamp()}.zip`,
    );

    const warnings = [];
    if (decryptFailures > 0) {
      warnings.push(`${decryptFailures} 件の復号失敗`);
    }
    if (imageFail > 0) {
      warnings.push(`${imageFail} 件の画像取得失敗`);
    }
    if (warnings.length > 0) {
      _setStatus("ダウンロード完了。" + warnings.join("、") + "。", "warning");
    } else {
      _setStatus("ダウンロード完了。", "success");
    }
  } catch (e) {
    _setStatus("エクスポートに失敗しました: " + (e.message || e), "danger");
  } finally {
    if (btn) btn.disabled = false;
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}
