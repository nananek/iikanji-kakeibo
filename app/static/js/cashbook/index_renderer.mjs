// Phase E3-F PR-D-4-2: 出納帳一覧 (cashbook/index.html) のクライアント描画。
//
// 旧実装はサーバが平文 JournalEntry.date で order_by/範囲 filter し、
// entry.date / entry.description / line.account.name を Jinja で出力していた。
// E2EE 化 (dual-read 撤去 #220) で平文列を DROP するため、一覧表示そのものを
// クライアント復号描画に移す (medical/index_renderer.mjs と同じ shell+renderer)。
//
// フロー: MK 復号 → fetchJournalsForYear(year) → source==="cashbook" 抽出 →
// 復号済み date 降順ソート → DOM 構築。科目名は accounts_meta (非暗号化メタ)
// で解決。削除は既存サーバ endpoint を htmx で叩く (動的生成行に htmx.process)。
//
// 監査代理閲覧時はオーナーの MK 暗号値をユーザーの MK では復号できないため、
// 早期 return + status info で空表示の旨を案内 (medical_index_renderer と同じ)。
//
// 年度スコープ: 旧 date_from/date_to 範囲 filter は fiscal_year セレクタに置換
// (平文 date に依存しない。fiscal_year は DROP 対象外の平文カラム)。


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


/**
 * 復号済み journals から出納帳 (source="cashbook") の一覧行を生成。
 *
 * 1 仕訳 = 1 行。借方/貸方科目名・金額 (借方合計) を組み立て、復号済み date の
 * 降順 (同日は entry_number 降順) でソートする。サーバ旧実装の
 * order_by(date.desc(), entry_number.desc()) と同じ並び。
 *
 * @param {Array<Object>} journals     fetchJournalsForYear の戻り値
 * @param {Object} accountsMeta        code → {name} (監査 Lv2 はマスク済み)
 * @returns {Array<Object>}
 */
export function buildCashbookRows(journals, accountsMeta) {
  const meta = accountsMeta || {};
  const rows = [];
  for (const entry of journals || []) {
    if (entry.source !== "cashbook") continue;
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
    rows.push({
      entry_id: entry.id,
      entry_number: entry.entry_number,
      date: entry.date || null,
      description: entry.description || "",
      debit_names: debitNames,
      credit_names: creditNames,
      amount,
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


function _setStatus(msg, type = "info") {
  const el = document.getElementById("cashbook-index-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("cashbook-index-status");
  if (el) el.classList.add("d-none");
}


function _renderRows(rows) {
  const tbody = document.getElementById("cashbook-index-tbody");
  const tableWrap = document.getElementById("cashbook-index-table");
  const empty = document.getElementById("cashbook-index-empty");
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

    _td(r.entry_number != null ? String(r.entry_number) : "");
    _td(_fmtDateYMD(r.date));
    _td(r.description);
    _td(r.debit_names.join(" / "));
    _td(r.credit_names.join(" / "));
    _td(_fmtYen(r.amount), "text-end");

    const actionTd = document.createElement("td");
    const group = document.createElement("div");
    group.className = "btn-group btn-group-sm";

    const editLink = document.createElement("a");
    editLink.href = "/cashbook/" + r.entry_id + "/edit";
    editLink.className = "btn btn-outline-secondary";
    editLink.innerHTML = '<i class="bi bi-pencil"></i>';
    group.appendChild(editLink);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-outline-danger";
    // 既存サーバ endpoint を htmx で叩く (CSRF は htmx:configRequest で自動付与
    // されるが、動的生成のため明示的に hx-headers でも付ける)。
    delBtn.setAttribute("hx-post", "/cashbook/" + r.entry_id + "/delete");
    delBtn.setAttribute("hx-confirm", "この仕訳を削除しますか？");
    delBtn.setAttribute("hx-target", "closest tr");
    delBtn.setAttribute("hx-swap", "outerHTML swap:0.3s");
    if (csrfToken) {
      delBtn.setAttribute("hx-headers", JSON.stringify({ "X-CSRFToken": csrfToken }));
    }
    delBtn.innerHTML = '<i class="bi bi-trash"></i>';
    group.appendChild(delBtn);

    actionTd.appendChild(group);
    tr.appendChild(actionTd);
    tbody.appendChild(tr);
  }

  // 動的生成した htmx 属性を有効化。
  if (globalThis.htmx && typeof globalThis.htmx.process === "function") {
    globalThis.htmx.process(tbody);
  }
}


async function _run() {
  const paramsEl = document.getElementById("cashbook-index-params");
  if (!paramsEl) return;
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (_e) {
    _setStatus("出納帳一覧の初期化に失敗しました。", "danger");
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number") {
    _setStatus("出納帳一覧の初期化に失敗しました (params)。", "danger");
    return;
  }

  let accountsMeta = {};
  const accountsEl = document.getElementById("cashbook-index-accounts-meta");
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (_e) {
    _setStatus("出納帳一覧の初期化に失敗しました (accounts)。", "danger");
    return;
  }

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
    if (params.is_audit_proxy) {
      _setStatus(
        "監査代理閲覧中です。オーナーの暗号化された出納帳データはあなたの暗号鍵では復号できないため、一覧は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      _renderRows([]);
      return;
    }
    _clearStatus();

    const journals = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const rows = buildCashbookRows(journals, accountsMeta);
    _renderRows(rows);
  } catch (e) {
    _setStatus("出納帳一覧の取得に失敗しました: " + (e.message || e), "danger");
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
