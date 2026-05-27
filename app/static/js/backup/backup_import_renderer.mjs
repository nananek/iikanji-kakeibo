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


function _clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}


// XSS 防止: ファイル由来の値を innerHTML で展開しない。textContent で構築する。
function _appendMeta(container, label, value) {
  const dt = document.createElement("dt");
  dt.className = "col-sm-3";
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.className = "col-sm-9";
  const code = document.createElement("code");
  code.textContent = String(value);
  dd.appendChild(code);
  container.appendChild(dt);
  container.appendChild(dd);
}


function _appendSummaryRow(tbody, label, n) {
  const tr = document.createElement("tr");
  const td1 = document.createElement("td");
  td1.textContent = label;
  const td2 = document.createElement("td");
  td2.className = "text-end";
  td2.textContent = String(n);
  tr.appendChild(td1);
  tr.appendChild(td2);
  tbody.appendChild(tr);
}


function _renderSummary(backup) {
  const data = backup.data || {};
  const cardEl = document.getElementById("restore-summary-card");
  const metaEl = document.getElementById("restore-summary-meta");
  const tbodyEl = document.getElementById("restore-summary-tbody");
  if (!cardEl || !metaEl || !tbodyEl) return;

  _clearChildren(metaEl);
  if (backup.version != null) _appendMeta(metaEl, "version", backup.version);
  if (backup.exported_at != null) _appendMeta(metaEl, "exported_at", backup.exported_at);
  if (backup.user_id != null) _appendMeta(metaEl, "user_id", backup.user_id);

  _clearChildren(tbodyEl);
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
  for (const [key, label] of tables) {
    _appendSummaryRow(tbodyEl, label, (data[key] || []).length);
  }
  // user_ai_config は 0 or 1
  _appendSummaryRow(tbodyEl, "AI 設定", data.user_ai_config != null ? 1 : 0);

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
    _lastPreviewedBackup = backup;
    // apply card を表示 (チェックボックスはユーザが付け直すまで未確認のまま)
    const applyCard = document.getElementById("restore-apply-card");
    if (applyCard) applyCard.classList.remove("d-none");
    const cb = document.getElementById("restore-apply-confirm");
    if (cb) cb.checked = false;
    const applyBtn = document.getElementById("restore-apply-btn");
    if (applyBtn) applyBtn.disabled = true;

    _setStatus(
      `読み込み完了 (version=${backup.version ?? "?"})。` +
      "下のカードで「全置換して復元する」を押すと DB が書き戻されます。",
      "success",
    );
  } catch (e) {
    _lastPreviewedBackup = null;
    _setStatus(
      "プレビューに失敗しました: " + (e.message || e),
      "danger",
    );
  } finally {
    if (btn) btn.disabled = false;
  }
}


// preview した最後の backup (apply で使う)
let _lastPreviewedBackup = null;


function _clearChildren(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}


function _renderRestoreResult(restored) {
  const card = document.getElementById("restore-result-card");
  const dl = document.getElementById("restore-result-counts");
  if (!card || !dl) return;
  _clearChildren(dl);
  const tables = (restored && restored.tables) || {};
  for (const [k, v] of Object.entries(tables)) {
    const dt = document.createElement("dt");
    dt.className = "col-sm-5";
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.className = "col-sm-7";
    dd.textContent = String(v);
    dl.appendChild(dt);
    dl.appendChild(dd);
  }
  card.classList.remove("d-none");
}


export async function runRestoreApply() {
  if (!_lastPreviewedBackup) {
    _setStatus("先に preview を実行してください。", "warning");
    return;
  }
  const cb = document.getElementById("restore-apply-confirm");
  if (!cb || !cb.checked) {
    _setStatus("同意チェックが必要です。", "warning");
    return;
  }
  if (!window.confirm(
    "既存データが全て削除され、バックアップ内容で置き換えられます。実行しますか?"
  )) {
    return;
  }

  const btn = document.getElementById("restore-apply-btn");
  if (btn) btn.disabled = true;
  _setStatus("DB に書き戻しています… (数秒〜数十秒かかります)", "info");

  try {
    // /api/v1 は CSRF 免除 + Web セッション認証 OK
    const resp = await fetch("/api/v1/backup/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(_lastPreviewedBackup),
    });
    let json = {};
    try { json = await resp.json(); } catch (_e) { /* ignore */ }
    if (!resp.ok) {
      throw new Error(json.error || `HTTP ${resp.status}`);
    }
    _renderRestoreResult(json.restored || {});
    _setStatus("リストアが完了しました。", "success");
    // apply card は再実行を促さないよう disable
    if (cb) cb.disabled = true;
  } catch (e) {
    _setStatus(
      "リストアに失敗しました: " + (e.message || e),
      "danger",
    );
  } finally {
    if (btn) btn.disabled = false;
  }
}
