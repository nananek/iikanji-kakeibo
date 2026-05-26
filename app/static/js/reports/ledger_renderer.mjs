// 総勘定元帳 (Ledger) のクライアント描画。
// MK 復号 → computeLedger → composeLedgerView → DOM 構築まで完結。
//
// carry_forward (前期繰越) は当面 0 とする (BCB 統合後 follow-up #221)。


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


function _fmtDateMD(isoDate) {
  if (!isoDate) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(isoDate);
  return m ? m[2] + "/" + m[3] : isoDate;
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("ledger-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("ledger-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("ledger_renderer: " + msg, logArg);
  _setStatus("元帳の初期化に失敗しました: " + msg, "danger");
}


function _td(content, opts = {}) {
  const td = document.createElement("td");
  if (opts.className) td.className = opts.className;
  if (opts.colSpan) td.colSpan = opts.colSpan;
  if (opts.style) td.setAttribute("style", opts.style);
  if (content instanceof Node) {
    td.appendChild(content);
  } else if (opts.bold) {
    const strong = document.createElement("strong");
    strong.textContent = content;
    td.appendChild(strong);
  } else {
    td.textContent = content;
  }
  return td;
}


function _renderCarryForwardRow(view) {
  // 大画面用 + モバイル用 2 行
  const cf = view.opening_balance;
  const tr = document.createElement("tr");
  tr.className = "table-secondary";
  tr.appendChild(_td(""));
  tr.appendChild(_td(""));
  tr.appendChild(_td("", { className: "d-mobile-none" }));
  tr.appendChild(_td("前期繰越", { bold: true }));
  tr.appendChild(_td("", { className: "d-mobile-none" }));
  tr.appendChild(_td(""));
  tr.appendChild(_td(""));
  const balTd = _td(_fmtYen(cf), {
    className: "text-end d-mobile-none" + (cf < 0 ? " text-danger" : ""),
    bold: true,
  });
  tr.appendChild(balTd);
  tr.appendChild(_td(""));
  return tr;
}


function _renderSummaryRow(view) {
  const tr = document.createElement("tr");
  tr.className = "table-dark";
  tr.appendChild(_td(""));
  tr.appendChild(_td("合計", { bold: true }));
  tr.appendChild(_td("", { className: "d-mobile-none" }));
  tr.appendChild(_td(""));
  tr.appendChild(_td("", { className: "d-mobile-none" }));
  tr.appendChild(_td(_fmtYen(view.total_debit), {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(_fmtYen(view.total_credit), {
    className: "text-end", bold: true,
  }));
  tr.appendChild(_td(_fmtYen(view.closing_balance), {
    className: "text-end d-mobile-none", bold: true,
  }));
  tr.appendChild(_td(""));
  return tr;
}


function _renderEntryRow(row) {
  const tr = document.createElement("tr");
  tr.setAttribute("data-ledger-entry", String(row.entry_id));

  // checkbox / 編集可否
  const cbTd = document.createElement("td");
  if (!row.is_readonly) {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "form-check-input ledger-cb";
    cb.name = "entry_ids";
    cb.value = String(row.entry_id);
    cbTd.appendChild(cb);
  }
  tr.appendChild(cbTd);

  // 日付 (MM/DD)
  tr.appendChild(_td(_fmtDateMD(row.date)));

  // 伝票番号
  const enTd = document.createElement("td");
  enTd.className = "d-mobile-none";
  if (row.entry_number != null) {
    const small = document.createElement("small");
    small.className = "text-muted";
    small.textContent = "#" + row.entry_number;
    enTd.appendChild(small);
  }
  tr.appendChild(enTd);

  // 摘要 + 証憑リンク
  const descTd = document.createElement("td");
  descTd.appendChild(document.createTextNode(row.description));
  if (row.voucher_id != null) {
    const tpl = document.getElementById("ledger-voucher-link-template");
    if (tpl && tpl.content && tpl.content.firstElementChild) {
      const a = tpl.content.firstElementChild.cloneNode(true);
      // template に "__VOUCHER_ID__" placeholder を含めてあるので、
      // onclick attribute と href を JS で書き換えるのではなく、
      // data-voucher-id だけ書き換えて click handler で参照
      a.setAttribute("data-voucher-id", String(row.voucher_id));
      descTd.appendChild(document.createTextNode(" "));
      descTd.appendChild(a);
    }
  }
  tr.appendChild(descTd);

  // 相手科目
  const counterTd = document.createElement("td");
  counterTd.className = "d-mobile-none";
  const small = document.createElement("small");
  small.textContent = row.counter_account_names || "";
  counterTd.appendChild(small);
  tr.appendChild(counterTd);

  // 借方 / 貸方
  tr.appendChild(_td(row.debit ? _fmtYen(row.debit) : "", { className: "text-end" }));
  tr.appendChild(_td(row.credit ? _fmtYen(row.credit) : "", { className: "text-end" }));

  // 残高
  tr.appendChild(_td(_fmtYen(row.balance), {
    className: "text-end d-mobile-none" + (row.balance < 0 ? " text-danger" : ""),
  }));

  // 編集ボタン
  const editTd = document.createElement("td");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-outline-secondary btn-sm py-0";
  btn.title = row.is_readonly ? "仕訳を閲覧" : "仕訳を編集";
  btn.setAttribute("data-ledger-edit", String(row.entry_id));
  const icon = document.createElement("i");
  icon.className = "bi " + (row.is_readonly ? "bi-eye" : "bi-pencil");
  btn.appendChild(icon);
  editTd.appendChild(btn);
  tr.appendChild(editTd);

  return tr;
}


function _renderMobileBalanceRow(balance, label = "残高") {
  // モバイル用の 1 行表示
  const tr = document.createElement("tr");
  tr.className = "d-mobile-row table-light";
  const td = document.createElement("td");
  td.colSpan = 6;
  td.className = "text-end py-1 border-0";
  const small = document.createElement("small");
  small.className = "text-muted";
  small.textContent = label;
  td.appendChild(small);
  const strong = document.createElement("strong");
  strong.className = "ms-2" + (balance < 0 ? " text-danger" : "");
  strong.textContent = " " + _fmtYen(balance);
  td.appendChild(strong);
  tr.appendChild(td);
  return tr;
}


function _renderPeriodGapRow() {
  const tr = document.createElement("tr");
  tr.className = "border-0";
  const td = document.createElement("td");
  td.colSpan = 9;
  td.className = "py-1 border-0";
  tr.appendChild(td);
  return tr;
}


function _renderView(view) {
  const tbody = document.getElementById("ledger-tbody");
  if (!tbody) return;
  while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
  const sortDesc = view.sort_order === "desc";

  if (sortDesc) {
    tbody.appendChild(_renderSummaryRow(view));
  } else {
    tbody.appendChild(_renderCarryForwardRow(view));
  }

  let prevPeriod = -1;
  let lastBalance = view.opening_balance;
  for (const row of view.rows) {
    if (row.fiscal_period !== prevPeriod && prevPeriod !== -1) {
      tbody.appendChild(_renderMobileBalanceRow(lastBalance));
      tbody.appendChild(_renderPeriodGapRow());
    }
    prevPeriod = row.fiscal_period;
    tbody.appendChild(_renderEntryRow(row));
    lastBalance = row.balance;
  }
  tbody.appendChild(_renderMobileBalanceRow(lastBalance));

  if (sortDesc) {
    tbody.appendChild(_renderCarryForwardRow(view));
  } else {
    tbody.appendChild(_renderSummaryRow(view));
  }
}


function _wireRowHandlers() {
  // 既存の global openImagePreview / openEditModal を発火
  const tbody = document.getElementById("ledger-tbody");
  if (!tbody) return;
  tbody.addEventListener("click", (ev) => {
    const editBtn = ev.target.closest("[data-ledger-edit]");
    if (editBtn && typeof globalThis.openEditModal === "function") {
      ev.preventDefault();
      const eid = parseInt(editBtn.getAttribute("data-ledger-edit"), 10);
      if (Number.isFinite(eid)) globalThis.openEditModal(eid);
      return;
    }
    const voucherLink = ev.target.closest("[data-voucher-id]");
    if (voucherLink && typeof globalThis.openImagePreview === "function") {
      ev.preventDefault();
      const vid = voucherLink.getAttribute("data-voucher-id");
      const urlTpl = document.getElementById(
        "ledger-voucher-image-url-template",
      );
      if (urlTpl) {
        const url = urlTpl.textContent.trim().replace("__VOUCHER_ID__", vid);
        globalThis.openImagePreview(url);
      }
    }
  });
}


async function _run() {
  const paramsEl = document.getElementById("ledger-server-params");
  if (!paramsEl) return;  // account_code 未選択時は params 自体出ない
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    _abort("failed to parse params", e);
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number") {
    _abort("invalid params (user_id/year)");
    return;
  }
  if (!params.account_code) {
    return;  // 科目未選択時
  }

  const accountsEl = document.getElementById("ledger-accounts-meta");
  const entriesEl = document.getElementById("ledger-entries-meta");
  let accountsMeta = {};
  let entriesMeta = {};
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    _abort("failed to parse accounts meta", e);
    return;
  }
  try {
    if (entriesEl) entriesMeta = JSON.parse(entriesEl.textContent);
  } catch (e) {
    _abort("failed to parse entries meta", e);
    return;
  }

  _wireRowHandlers();

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { computeLedger },
      { composeLedgerView },
      { fetchBalanceCacheBlobs },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/ledger.js"),
      import(getStaticRoot() + "js/crypto/reports/ledger_view.js"),
      import(getStaticRoot() + "js/crypto/balance_cache_blobs_client.js"),
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
        "監査代理閲覧中です。オーナーの暗号化された仕訳はあなたの暗号鍵では復号できないため、元帳は空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    const meta = accountsMeta[params.account_code] || {};
    const normalBalance = meta.normal_balance
      || params.normal_balance
      || "debit";

    const pf = params.pf || 0;
    // #230: BCB fetch と journals fetch を並列化。pf=0 (期首) のときは
    // BCB 不要なので空 dict を即 resolve、pf>=1 で前 period の blob を取る。
    const bcbPromise = pf >= 1
      ? fetchBalanceCacheBlobs({
          client, userId: params.user_id, fiscalYear: params.year,
        }).catch((e) => {
          console.warn(
            "ledger_renderer: BCB fetch failed, openingBalance=0", e,
          );
          return {};
        })
      : Promise.resolve({});
    const journalsPromise = fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const [blobs, entries] = await Promise.all([bcbPromise, journalsPromise]);

    let openingBalance = 0;
    if (pf >= 1) {
      const cacheForPeriod = blobs[pf - 1];
      if (cacheForPeriod) {
        const pair = cacheForPeriod[params.account_code];
        if (Array.isArray(pair) && pair.length >= 2) {
          const [cumD, cumC] = pair;
          openingBalance = normalBalance === "debit"
            ? (cumD - cumC) : (cumC - cumD);
        }
      }
    }
    const ledgerResult = computeLedger(entries, {
      accountCode: params.account_code,
      normalBalance,
      openingBalance,
      fiscalPeriodFrom: pf,
      fiscalPeriodTo: params.pt || 15,
      includeClosing: true,
    });
    const view = composeLedgerView(ledgerResult, {
      accountsMeta, entriesMeta, sortOrder: params.sort,
    });
    _renderView(view);
  } catch (e) {
    _setStatus("元帳の取得に失敗しました: " + (e.message || e), "danger");
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
