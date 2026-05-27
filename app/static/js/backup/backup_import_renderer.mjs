// Phase v5 BU-4a: バックアップファイルから復号 + preview のみ。
//
// .json (平文) は直接 parse、.ikbackup は parseEncryptedFile で復号してから parse。
// DB 書き戻しは行わない (BU-4b/c で別 PR)。
//
// 表示する件数:
//   accounts, journal_entries, journal_entry_lines, medical_expenses,
//   fiscal_closes, balance_cache_blobs, vouchers, ai_drafts,
//   webhook_configs, tax_form_mappings, csv_column_profiles,
//   user_ai_config (オブジェクト, 0 or 1 として件数化)


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("restore-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " small";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("restore-status");
  if (el) el.classList.add("d-none");
}


function _readFile() {
  const el = document.getElementById("restore-file");
  if (!el || !el.files || el.files.length === 0) return null;
  return el.files[0];
}


function _readPassphrase() {
  const el = document.getElementById("restore-passphrase");
  return el ? (el.value || "") : "";
}


async function _readAsArrayBuffer(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error("FileReader failed"));
    reader.readAsArrayBuffer(file);
  });
}


function _summaryRow(label, n) {
  return `<tr><td>${label}</td><td class="text-end">${n}</td></tr>`;
}


function _renderSummary(backup) {
  const data = backup.data || {};
  const cardEl = document.getElementById("restore-summary-card");
  const metaEl = document.getElementById("restore-summary-meta");
  const tbodyEl = document.getElementById("restore-summary-tbody");
  if (!cardEl || !metaEl || !tbodyEl) return;

  metaEl.innerHTML = "";
  if (backup.version) {
    metaEl.innerHTML += `<dt class="col-sm-3">version</dt><dd class="col-sm-9"><code>${backup.version}</code></dd>`;
  }
  if (backup.exported_at) {
    metaEl.innerHTML += `<dt class="col-sm-3">exported_at</dt><dd class="col-sm-9"><code>${backup.exported_at}</code></dd>`;
  }
  if (backup.user_id != null) {
    metaEl.innerHTML += `<dt class="col-sm-3">user_id</dt><dd class="col-sm-9"><code>${backup.user_id}</code></dd>`;
  }

  const tables = [
    ["accounts", "勘定科目"],
    ["fiscal_closes", "月次確定"],
    ["journal_entries", "仕訳伝票"],
    ["journal_entry_lines", "仕訳明細"],
    ["medical_expenses", "医療費"],
    ["balance_cache_blobs", "残高キャッシュ"],
    ["vouchers", "証憑 (画像)"],
    ["ai_drafts", "AI 下書き"],
    ["webhook_configs", "Webhook 設定"],
    ["tax_form_mappings", "決算書マッピング"],
    ["csv_column_profiles", "CSV プロファイル"],
  ];
  tbodyEl.innerHTML = tables.map(
    ([key, label]) => _summaryRow(label, (data[key] || []).length),
  ).join("");
  // user_ai_config は 0 or 1
  tbodyEl.innerHTML += _summaryRow(
    "AI 設定", data.user_ai_config != null ? 1 : 0,
  );

  cardEl.classList.remove("d-none");
}


async function _parseFile(file, passphrase) {
  const buf = await _readAsArrayBuffer(file);
  const isIkbackup = /\.ikbackup$/i.test(file.name);
  if (isIkbackup) {
    if (!passphrase) {
      throw new Error("暗号化アーカイブにはパスフレーズが必要です");
    }
    const [{ decryptBackupArchive }, { loadHashWasm }] = await Promise.all([
      import(getStaticRoot() + "js/crypto/backup_archive.js"),
      import(getStaticRoot() + "js/crypto/hash_wasm_loader.js"),
    ]);
    await loadHashWasm();
    _setStatus(
      "パスフレーズから鍵を派生して復号中… (数秒かかります)", "info",
    );
    const plaintextBytes = await decryptBackupArchive(
      new Uint8Array(buf), passphrase,
    );
    const text = new TextDecoder("utf-8", { fatal: true }).decode(plaintextBytes);
    return JSON.parse(text);
  }
  // 平文 JSON
  const text = new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(buf));
  return JSON.parse(text);
}


export async function runRestorePreview() {
  _clearStatus();
  const btn = document.getElementById("restore-preview-btn");
  if (btn) btn.disabled = true;

  try {
    const file = _readFile();
    if (!file) {
      _setStatus("ファイルが選択されていません。", "warning");
      return;
    }
    const passphrase = _readPassphrase();

    _setStatus("ファイルを読み込んでいます…", "info");
    const backup = await _parseFile(file, passphrase);

    if (!backup || typeof backup !== "object") {
      throw new Error("ファイルの構造が不正です (top-level object でない)");
    }
    if (!backup.data || typeof backup.data !== "object") {
      throw new Error("ファイルの構造が不正です (data フィールドなし)");
    }

    _renderSummary(backup);
    _setStatus(
      `読み込み完了 (version=${backup.version ?? "?"})。` +
      "DB への書き戻しは次の PR で対応予定です。",
      "success",
    );
  } catch (e) {
    _setStatus(
      "プレビューに失敗しました: " + (e.message || e),
      "danger",
    );
  } finally {
    if (btn) btn.disabled = false;
  }
}
