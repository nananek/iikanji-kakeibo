// Phase E3-F PR-D-4-3: 仕訳帳一覧 (journal/index.html) のクライアント描画。
//
// 旧実装はサーバが平文 JournalEntry.date で order_by/範囲 filter し、
// description.ilike で摘要検索し、entry.date/description/line.account.name を
// Jinja で出力していた。E2EE 化 (dual-read 撤去 #220) で平文列を DROP するため、
// 一覧表示そのものをクライアント復号描画に移す (cashbook/medical と同じ
// shell+renderer)。
//
// フロー: MK 復号 → fetchJournalsForYear(year) → buildJournalRows で行整形
// (編集可否 modifiable / 借方貸方科目名 / 金額 / ソースバッジ) → DOM 構築。
// 摘要検索はクライアント側で復号済み行を絞り込む (平文 description.ilike の置換)。
// 一括削除は既存 bulkSelect (Alpine) + bulk_delete endpoint を再利用。削除は
// 既存 endpoint を htmx で叩く (動的生成行に htmx.process)。
//
// modifiable はサーバ check_entry_modifiable と等価:
//   modifiable = !is_closing
//                && !(period <= closed_period[year])   // period = fiscal_month ?? fiscal_period ?? date.month
// これは UI ヒント (チェックボックス/編集ボタン vs ロックアイコン) であり、
// 実際の削除・編集はサーバ側 endpoint が再検証する (権威はサーバ)。
//
// 監査代理閲覧時はオーナーの MK 暗号値をユーザーの MK では復号できないため、
// 早期 return + status info で空表示の旨を案内 (cashbook/medical と同じ)。
// year スコープ: 旧 date_from/date_to 範囲 filter は fiscal_year セレクタに置換。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _fmtYen(n) {
  return "¥" + (n || 0).toLocaleString();
}


function _fmtDateYMD(iso) {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? m[1] + "/" + m[2] + "/" + m[3] : iso;
}


function _accountName(code, accountsMeta) {
  if (!code) return "";
  const meta = accountsMeta && accountsMeta[code];
  return meta && meta.name ? meta.name : code;
}


function _yearOf(entry) {
  if (entry.fiscal_year != null) return entry.fiscal_year;
  const m = /^(\d{4})-/.exec(entry.date || "");
  return m ? Number(m[1]) : null;
}


function _periodOf(entry) {
  if (entry.fiscal_month != null) return entry.fiscal_month;
  if (entry.fiscal_period != null) return entry.fiscal_period;
  const m = /^\d{4}-(\d{2})-/.exec(entry.date || "");
  return m ? Number(m[1]) : null;
}


/**
 * 復号済み journals から仕訳帳一覧行を生成。
 *
 * サーバ check_entry_modifiable と等価の編集可否 (modifiable) を付与し、
 * date 降順 / entry_number 降順でソート (旧 order_by と同じ並び)。全 source を
 * 含む (仕訳帳は出納帳・AI・取込も表示)。
 *
 * @param {Array<Object>} journals     fetchJournalsForYear の戻り値
 * @param {Object} accountsMeta        code → {name} (監査 Lv2 はマスク済み)
 * @param {Object} opts
 * @param {Object} opts.closedPeriods  {year: closed_period}
 * @returns {Array<Object>}
 */
export function buildJournalRows(journals, accountsMeta, opts = {}) {
  const meta = accountsMeta || {};
  const closedPeriods = opts.closedPeriods || {};
  const rows = [];
  for (const entry of journals || []) {
    const debitNames = [];
    const creditNames = [];
    let amount = 0;
    for (const line of entry.lines || []) {
      if ((line.debit || 0) > 0) {
        debitNames.push(_accountName(line.account_code, meta));
        amount += line.debit || 0;
      }
      if ((line.credit || 0) > 0) {
        creditNames.push(_accountName(line.account_code, meta));
      }
    }

    const isClosing = !!entry.is_closing;
    const year = _yearOf(entry);
    const period = _periodOf(entry);
    const closed = closedPeriods[year];
    const periodLocked =
      closed != null && period != null && period <= closed;
    const modifiable = !isClosing && !periodLocked;

    const vouchers = entry.vouchers || [];
    rows.push({
      entry_id: entry.id,
      entry_number: entry.entry_number,
      date: entry.date || null,
      description: entry.description || "",
      debit_names: debitNames,
      credit_names: creditNames,
      amount,
      source: entry.source || "",
      is_closing: isClosing,
      modifiable,
      has_voucher: vouchers.length > 0,
      voucher_id: vouchers.length > 0 ? vouchers[0].id : null,
    });
  }
  rows.sort((a, b) => {
    const ad = a.date || "";
    const bd = b.date || "";
    if (ad !== bd) return ad < bd ? 1 : -1; // date 降順
    return (b.entry_number || 0) - (a.entry_number || 0); // entry_number 降順
  });
  return rows;
}


const _SOURCE_BADGE = {
  cashbook: { cls: "bg-info", label: "出納帳" },
  ai_receipt: { cls: "bg-success", label: "AI" },
  csv: { cls: "bg-primary", label: "CSV" },
  ofx: { cls: "bg-primary", label: "OFX" },
  web: { cls: "bg-primary", label: "Web" },
};


function _sourceBadge(row) {
  if (row.is_closing) return { cls: "bg-dark", label: "損益振替" };
  return _SOURCE_BADGE[row.source] || { cls: "bg-secondary", label: "仕訳" };
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("journal-index-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("journal-index-status");
  if (el) el.classList.add("d-none");
}


// 描画状態 (検索フィルタ用に全行を保持)。
let _allRows = [];


function _filteredRows() {
  const input = document.getElementById("journal-search");
  const q = (input && input.value ? input.value : "").trim().toLowerCase();
  if (!q) return _allRows;
  return _allRows.filter((r) => (r.description || "").toLowerCase().includes(q));
}


function _renderRows(rows) {
  const tbody = document.getElementById("journal-index-tbody");
  const tableWrap = document.getElementById("journal-index-table");
  const empty = document.getElementById("journal-index-empty");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

  if (tableWrap) tableWrap.classList.toggle("d-none", rows.length === 0);
  if (empty) empty.classList.toggle("d-none", rows.length !== 0);

  const bulkForm = document.getElementById("bulkForm");
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

    // チェックボックス (modifiable のみ)
    const cbTd = document.createElement("td");
    if (r.modifiable) {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "form-check-input entry-cb";
      cb.name = "entry_ids";
      cb.value = String(r.entry_id);
      if (r.has_voucher) cb.setAttribute("data-has-voucher", "true");
      cb.addEventListener("change", () => {
        if (bulkForm) bulkForm.dispatchEvent(new CustomEvent("drag-select-update"));
      });
      cbTd.appendChild(cb);
    }
    tr.appendChild(cbTd);

    _td(r.entry_number != null ? String(r.entry_number) : "");
    _td(_fmtDateYMD(r.date));
    _td(r.description);
    _td(r.debit_names.join(" / "));
    _td(r.credit_names.join(" / "));
    _td(_fmtYen(r.amount), "text-end");

    // 入力元バッジ (+ AI 証憑画像リンク)
    const srcTd = document.createElement("td");
    srcTd.className = "d-mobile-none";
    const badge = _sourceBadge(r);
    const span = document.createElement("span");
    span.className = "badge " + badge.cls;
    span.textContent = badge.label;
    srcTd.appendChild(span);
    if (r.source === "ai_receipt" && r.has_voucher && r.voucher_id != null) {
      srcTd.appendChild(document.createTextNode(" "));
      const link = document.createElement("a");
      link.href = "#";
      link.className = "text-success";
      link.title = "証憑画像";
      link.innerHTML = '<i class="bi bi-image"></i>';
      const url = "/ai-journal/voucher/" + r.voucher_id + "/image";
      link.addEventListener("click", (e) => {
        e.preventDefault();
        if (typeof globalThis.openImagePreview === "function") {
          globalThis.openImagePreview(url);
        }
      });
      srcTd.appendChild(link);
    }
    tr.appendChild(srcTd);

    // 操作 (編集/削除 or ロックアイコン)
    const actionTd = document.createElement("td");
    if (r.modifiable) {
      const group = document.createElement("div");
      group.className = "btn-group btn-group-sm";

      const editLink = document.createElement("a");
      editLink.className = "btn btn-outline-secondary";
      editLink.href =
        (r.source === "cashbook" ? "/cashbook/" : "/journal/") +
        r.entry_id + "/edit";
      editLink.innerHTML = '<i class="bi bi-pencil"></i>';
      group.appendChild(editLink);

      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-outline-danger";
      delBtn.setAttribute("hx-post", "/journal/" + r.entry_id + "/delete");
      delBtn.setAttribute(
        "hx-confirm",
        r.has_voucher
          ? "この仕訳には証憑画像が紐づいています。削除すると証憑が未紐付けになります。削除しますか？"
          : "この仕訳を削除しますか？",
      );
      delBtn.setAttribute("hx-target", "closest tr");
      delBtn.setAttribute("hx-swap", "outerHTML swap:0.3s");
      if (csrfToken) {
        delBtn.setAttribute("hx-headers", JSON.stringify({ "X-CSRFToken": csrfToken }));
      }
      delBtn.innerHTML = '<i class="bi bi-trash"></i>';
      group.appendChild(delBtn);

      actionTd.appendChild(group);
    } else {
      const lock = document.createElement("i");
      lock.className = "bi bi-lock-fill text-muted";
      lock.title = "変更不可";
      actionTd.appendChild(lock);
    }
    tr.appendChild(actionTd);

    tbody.appendChild(tr);
  }

  // 動的生成した htmx 属性を有効化。
  if (globalThis.htmx && typeof globalThis.htmx.process === "function") {
    globalThis.htmx.process(tbody);
  }
  // drag 選択を (再) 初期化。
  if (typeof globalThis.initDragSelect === "function") {
    globalThis.initDragSelect(".table", ".entry-cb", () => {
      if (bulkForm) bulkForm.dispatchEvent(new CustomEvent("drag-select-update"));
    });
  }
  // bulkSelect (Alpine) のカウントを更新。
  if (bulkForm) bulkForm.dispatchEvent(new CustomEvent("drag-select-update"));
}


function _rerender() {
  _renderRows(_filteredRows());
}


async function _run() {
  const paramsEl = document.getElementById("journal-index-params");
  if (!paramsEl) return;
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (_e) {
    _setStatus("仕訳帳一覧の初期化に失敗しました。", "danger");
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number") {
    _setStatus("仕訳帳一覧の初期化に失敗しました (params)。", "danger");
    return;
  }

  let accountsMeta = {};
  const accountsEl = document.getElementById("journal-index-accounts-meta");
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (_e) {
    _setStatus("仕訳帳一覧の初期化に失敗しました (accounts)。", "danger");
    return;
  }

  let extra = {};
  const extraEl = document.getElementById("journal-index-extra");
  try {
    if (extraEl) extra = JSON.parse(extraEl.textContent);
  } catch (_e) {
    extra = {};
  }

  // 摘要検索: クライアント側で復号済み行を絞り込む。
  const searchInput = document.getElementById("journal-search");
  if (searchInput) searchInput.addEventListener("input", _rerender);

  let client;
  try {
    const [{ SharedCryptoClient }, { fetchJournalsForYear }] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
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
    _clearStatus();

    const journals = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    _allRows = buildJournalRows(journals, accountsMeta, {
      closedPeriods: extra.closed_periods || {},
    });
    _rerender();
  } catch (e) {
    _setStatus("仕訳帳一覧の取得に失敗しました: " + (e.message || e), "danger");
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
