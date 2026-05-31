// Phase E3-F PR-D-6-3b-2: 取込履歴 (journal/batches.html) のクライアント描画。
//
// 旧実装はサーバが平文 JournalEntry.date / source を集計してテーブルを描画
// していた。E2EE 化 (date / source 列は D-6-5 で DROP) のため、バッチ一覧を
// GET /api/v1/journals/batches から取得し、復号 blob から種別ラベル (source)
// と日付範囲 (date_from / date_to) をクライアントで組み立てる。件数 / 取込
// 日時 / 削除可否は保持列由来でサーバ (API) が算出する。
//
// 監査代理閲覧時はオーナーの MK 暗号値を復号できないため、早期 return +
// status info で空表示を案内する (journal 一覧 index_renderer と同じ)。削除は
// 既存 POST /journal/batches/<batch_id>/delete フォームを動的生成して再利用。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _fmtDateYMD(iso) {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? m[1] + "-" + m[2] + "-" + m[3] : iso;
}


function _fmtDateTime(iso) {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso);
  return m ? m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] : iso;
}


// 旧 batches.html の source_labels.get(source, source) と同じ挙動。
let _sourceLabels = {};


function _sourceLabel(source) {
  if (Object.prototype.hasOwnProperty.call(_sourceLabels, source)) {
    return _sourceLabels[source];
  }
  return source || "";
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("batches-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("batches-status");
  if (el) el.classList.add("d-none");
}


function _renderRows(rows) {
  const tbody = document.getElementById("batches-tbody");
  const tableWrap = document.getElementById("batches-table");
  const empty = document.getElementById("batches-empty");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

  if (tableWrap) tableWrap.classList.toggle("d-none", rows.length === 0);
  if (empty) empty.classList.toggle("d-none", rows.length !== 0);

  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  for (const r of rows) {
    const tr = document.createElement("tr");

    function _td(text, cls) {
      const td = document.createElement("td");
      if (cls) td.className = cls;
      td.textContent = text;
      tr.appendChild(td);
      return td;
    }

    _td(_fmtDateTime(r.imported_at));
    _td(_sourceLabel(r.source));
    _td((r.count != null ? r.count : 0) + "件", "text-end");
    _td(_fmtDateYMD(r.date_from) + " 〜 " + _fmtDateYMD(r.date_to));

    // 操作 (削除フォーム or 無効ボタン or "—")
    const actionTd = document.createElement("td");
    actionTd.className = "text-center";
    if (r.is_closing) {
      const span = document.createElement("span");
      span.className = "text-muted small";
      span.textContent = "—";
      actionTd.appendChild(span);
    } else if (!r.deletable) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-outline-danger btn-sm";
      btn.disabled = true;
      btn.title = r.delete_reason || "";
      btn.innerHTML = '<i class="bi bi-trash"></i> 取消';
      actionTd.appendChild(btn);
    } else {
      const form = document.createElement("form");
      form.method = "POST";
      form.action = "/journal/batches/" + encodeURIComponent(r.batch_id) + "/delete";
      form.style.display = "inline";
      form.addEventListener("submit", (e) => {
        const msg =
          "このインポート（" + r.count + "件）をすべて削除しますか？" +
          "この操作は取り消せません。";
        if (!globalThis.confirm(msg)) e.preventDefault();
      });
      if (csrfToken) {
        const csrf = document.createElement("input");
        csrf.type = "hidden";
        csrf.name = "csrf_token";
        csrf.value = csrfToken;
        form.appendChild(csrf);
      }
      const btn = document.createElement("button");
      btn.type = "submit";
      btn.className = "btn btn-outline-danger btn-sm";
      btn.innerHTML = '<i class="bi bi-trash"></i> 取消';
      form.appendChild(btn);
      actionTd.appendChild(form);
    }
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }
}


async function _run() {
  const paramsEl = document.getElementById("batches-params");
  if (!paramsEl) return;
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (_e) {
    _setStatus("取込履歴の初期化に失敗しました。", "danger");
    return;
  }
  if (typeof params.user_id !== "number") {
    _setStatus("取込履歴の初期化に失敗しました (params)。", "danger");
    return;
  }

  const labelsEl = document.getElementById("batches-source-labels");
  try {
    if (labelsEl) _sourceLabels = JSON.parse(labelsEl.textContent) || {};
  } catch (_e) {
    _sourceLabels = {};
  }

  let client;
  try {
    const [{ SharedCryptoClient }, { fetchBatches }] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/batches_client.js"),
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
    if (params.is_audit_proxy) {
      _setStatus(
        "監査代理閲覧中です。オーナーの暗号化された取込履歴はあなたの暗号鍵では復号できないため、一覧は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      _renderRows([]);
      return;
    }
    _clearStatus();

    const rows = await fetchBatches({ client, userId: params.user_id });
    _renderRows(rows);
  } catch (e) {
    _setStatus("取込履歴の取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
