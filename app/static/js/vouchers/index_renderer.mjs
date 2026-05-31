// Phase E3-F PR-D-4-4: 証憑一覧 (vouchers/index.html) のクライアント描画。
//
// 旧実装はサーバが紐付け仕訳の平文 JournalEntry.date/description と
// JournalEntryLine.debit_amount 合計を読んで一覧 (電帳法 検索要件: 日付/金額/
// 摘要) を描画していた。E2EE 化 (dual-read 撤去 #220) で平文列を DROP するため、
// 仕訳由来の表示・検索をクライアント復号描画に移す (journal/cashbook と同じ
// shell+renderer)。
//
// データソース: サーバ shell が証憑メタ (非暗号化: id / journal_entry_id /
// entry_number / fiscal_year / uploaded_at / has_hash) を JSON で渡す。
// クライアントは紐付け仕訳の fiscal_year 群を fetchJournalsForYear で取得・復号し、
// entry_id → {date, description, amount} を解決してカードを組み立てる。
// 日付/金額/摘要の絞り込み (電帳法) はクライアント側で行う。
//
// 監査代理閲覧 (proxy) / MK ロック時はオーナー仕訳を復号できないが、証憑画像
// 自体は E2EE 暗号化されていない (ストレージのファイル) ため、仕訳メタ
// (entry_number / uploaded_at / サムネイル) のみのカードを描画する。日付検索は
// uploaded_at ベース、金額/摘要検索は紐付け仕訳の復号が要るため無効化する。


function getSharedWorkerUrl() {
  return globalThis.IIKANJI_SHARED_WORKER_URL || "/static/js/crypto/shared-worker.js";
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _fmtYen(n) {
  return "¥" + (n || 0).toLocaleString();
}


function _datePart(iso) {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? m[1] + "-" + m[2] + "-" + m[3] : null;
}


function _fmtYMD(d) {
  if (!d) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d);
  return m ? m[1] + "/" + m[2] + "/" + m[3] : d;
}


function _fmtYMDHM(iso) {
  if (!iso) return "";
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(iso);
  if (!m) return _fmtYMD(_datePart(iso));
  return m[1] + "/" + m[2] + "/" + m[3] + " " + m[4] + ":" + m[5];
}


function _daysBetween(laterDate, earlierDate) {
  // どちらも "YYYY-MM-DD"。UTC 正午基準で日数差 (later - earlier) を返す。
  const a = /^(\d{4})-(\d{2})-(\d{2})/.exec(laterDate);
  const b = /^(\d{4})-(\d{2})-(\d{2})/.exec(earlierDate);
  if (!a || !b) return null;
  const ta = Date.UTC(Number(a[1]), Number(a[2]) - 1, Number(a[3]));
  const tb = Date.UTC(Number(b[1]), Number(b[2]) - 1, Number(b[3]));
  return Math.floor((ta - tb) / 86400000);
}


/**
 * 証憑メタ + 復号済み仕訳マップからカード行を生成。
 *
 * @param {Array<Object>} voucherMeta  [{id, journal_entry_id, entry_number, fiscal_year, uploaded_at, has_hash}]
 * @param {Map<number,Object>} entryMap entry_id → {date, description, amount}
 * @returns {Array<Object>}            date 降順 / voucher_id 降順
 */
export function buildVoucherCards(voucherMeta, entryMap) {
  const map = entryMap || new Map();
  const cards = [];
  for (const v of voucherMeta || []) {
    // id 系は script タグ (DOM text) 由来。URL sink (img.src/form.action) に
    // 流す前に数値へ強制し、非整数は除外する (XSS 経路の遮断 + 防御的検証)。
    const voucherId = Number(v.id);
    if (!Number.isInteger(voucherId)) continue;
    const journalEntryId = v.journal_entry_id == null ? null : Number(v.journal_entry_id);
    const attached = journalEntryId != null && Number.isInteger(journalEntryId);
    const entry = attached ? map.get(journalEntryId) : null;
    const uploadedDate = _datePart(v.uploaded_at);
    const entryDate = entry && entry.date ? _datePart(entry.date) : null;
    const effectiveDate = entryDate || uploadedDate || null;
    let overdueDays = null;
    if (attached && entryDate && uploadedDate) {
      const d = _daysBetween(uploadedDate, entryDate);
      if (d != null && d > 67) overdueDays = d;
    }
    cards.push({
      voucher_id: voucherId,
      journal_entry_id: attached ? journalEntryId : null,
      attached,
      encrypted: !!v.encrypted,
      entry_number: v.entry_number ?? (entry ? entry.entry_number : null),
      effective_date: effectiveDate,
      entry_date: entryDate,
      description: entry ? (entry.description || "") : "",
      amount: entry && entry.amount != null ? entry.amount : null,
      uploaded_at: v.uploaded_at || null,
      has_hash: !!v.has_hash,
      overdue_days: overdueDays,
    });
  }
  cards.sort((a, b) => {
    const ad = a.effective_date || "";
    const bd = b.effective_date || "";
    if (ad !== bd) return ad < bd ? 1 : -1; // 降順
    return (b.voucher_id || 0) - (a.voucher_id || 0);
  });
  return cards;
}


/**
 * 電帳法 検索条件でカードを絞り込む。
 *
 * - date_from/date_to: effective_date 範囲 (紐付け=仕訳日、未紐付け=保存日)
 * - amount_from/amount_to: amount 範囲。amount が無いカード (未紐付け / 未復号)
 *   は金額条件指定時に除外 (サーバ outerjoin の挙動と同じ)
 * - search: description 部分一致。description が空のカードは検索指定時に除外
 *   (サーバが JournalEntry.description.ilike で join を要求するのと同じ)
 *
 * @param {Array<Object>} cards
 * @param {Object} f  {dateFrom, dateTo, amountFrom, amountTo, search}
 * @returns {Array<Object>}
 */
export function filterVoucherCards(cards, f = {}) {
  const dateFrom = f.dateFrom || "";
  const dateTo = f.dateTo || "";
  const amountFrom = f.amountFrom === "" || f.amountFrom == null ? null : Number(f.amountFrom);
  const amountTo = f.amountTo === "" || f.amountTo == null ? null : Number(f.amountTo);
  const search = (f.search || "").trim().toLowerCase();
  return (cards || []).filter((c) => {
    if (dateFrom && (!c.effective_date || c.effective_date < dateFrom)) return false;
    if (dateTo && (!c.effective_date || c.effective_date > dateTo)) return false;
    if (amountFrom != null) {
      if (c.amount == null || c.amount < amountFrom) return false;
    }
    if (amountTo != null) {
      if (c.amount == null || c.amount > amountTo) return false;
    }
    if (search) {
      if (!c.description || !c.description.toLowerCase().includes(search)) return false;
    }
    return true;
  });
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("vouchers-index-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("vouchers-index-status");
  if (el) el.classList.add("d-none");
}


// 描画状態。
let _allCards = [];
let _canDelete = false;
let _csrfToken = "";
// E4 PR-C2: 暗号化証憑の復号表示用。MK 解錠かつ本人モード時のみ _client を
// 保持し (ページ生存中はクローズしない)、サムネ/本体を fetch + 復号する。
let _client = null;
let _userId = null;
let _canDecrypt = false;
let _decryptImage = null;  // voucher_download.fetchAndDecryptVoucherImage
let _sniffMime = null;     // voucher_download.sniffImageMime
let _fullPreviewUrl = null;  // 直近の本体プレビュー blob URL (revoke 用)


function _imgUrl(voucherId, thumb) {
  // voucherId は数値だが、念のため encodeURIComponent で URL sink を明示的に
  // サニタイズ (CodeQL js/xss-through-dom 対策・防御的)。
  const id = encodeURIComponent(String(voucherId));
  return "/ai-journal/voucher/" + id + "/image" + (thumb ? "?size=thumb" : "");
}


function _badge(cls, label, title) {
  const span = document.createElement("span");
  span.className = "badge " + cls;
  span.textContent = label;
  if (title) span.title = title;
  return span;
}


function _postForm(action, confirmMsg) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = action;
  form.className = "d-inline ms-1";
  if (confirmMsg) {
    form.addEventListener("submit", (e) => {
      if (!globalThis.confirm(confirmMsg)) e.preventDefault();
    });
  }
  const csrf = document.createElement("input");
  csrf.type = "hidden";
  csrf.name = "csrf_token";
  csrf.value = _csrfToken;
  form.appendChild(csrf);
  return form;
}


function _lockPlaceholder(label) {
  const ph = document.createElement("div");
  ph.className = "text-muted border rounded d-flex flex-column align-items-center justify-content-center";
  ph.style.height = "200px";
  ph.innerHTML = '<i class="bi bi-lock-fill" style="font-size:2rem;"></i>';
  const span = document.createElement("div");
  span.className = "small mt-2";
  span.textContent = label;
  ph.appendChild(span);
  return ph;
}


function _openDecryptedFull(voucherId) {
  // 本体を復号して blob URL でプレビュー表示。直近の URL は revoke して
  // メモリリークを防ぐ (毎クリックで新規 blob URL を作るため)。
  _decryptImage({ client: _client, userId: _userId, voucherId, thumb: false })
    .then((bytes) => {
      if (_fullPreviewUrl) {
        try { URL.revokeObjectURL(_fullPreviewUrl); } catch (_e) { /* ignore */ }
      }
      _fullPreviewUrl = URL.createObjectURL(
        new Blob([bytes], { type: _sniffMime(bytes) }),
      );
      if (typeof globalThis.openImagePreview === "function") {
        globalThis.openImagePreview(_fullPreviewUrl);
      }
    })
    .catch(() => _setStatus("証憑の復号に失敗しました。", "danger"));
}


function _renderCardImage(wrap, c) {
  // レガシー平文証憑: サーバが画像を直接配信するので従来通り img.src。
  if (!c.encrypted) {
    const link = document.createElement("a");
    link.href = "#";
    link.addEventListener("click", (e) => {
      e.preventDefault();
      if (typeof globalThis.openImagePreview === "function") {
        globalThis.openImagePreview(_imgUrl(c.voucher_id, false));
      }
    });
    const img = document.createElement("img");
    img.src = _imgUrl(c.voucher_id, true);
    img.className = "img-fluid rounded";
    img.style.maxHeight = "200px";
    img.style.cursor = "pointer";
    img.alt = "証憑";
    img.loading = "lazy";
    link.appendChild(img);
    wrap.appendChild(link);
    return;
  }

  // 暗号化証憑: MK 解錠かつ本人モードでのみ復号表示。
  if (!_canDecrypt || !_decryptImage) {
    wrap.appendChild(_lockPlaceholder("暗号鍵が必要です"));
    return;
  }

  const link = document.createElement("a");
  link.href = "#";
  link.style.cursor = "pointer";
  link.addEventListener("click", (e) => {
    e.preventDefault();
    _openDecryptedFull(c.voucher_id);
  });
  const img = document.createElement("img");
  img.className = "img-fluid rounded";
  img.style.maxHeight = "200px";
  img.style.cursor = "pointer";
  img.alt = "証憑 (復号中…)";
  img.loading = "lazy";
  link.appendChild(img);
  wrap.appendChild(link);

  // サムネを非同期復号 → blob URL。失敗時はロック表示に差し替え。
  _decryptImage({ client: _client, userId: _userId, voucherId: c.voucher_id, thumb: true })
    .then((bytes) => {
      const url = URL.createObjectURL(
        new Blob([bytes], { type: _sniffMime(bytes) }),
      );
      // 表示完了後に revoke してメモリリークを防ぐ。
      img.onload = () => { try { URL.revokeObjectURL(url); } catch (_e) { /* ignore */ } };
      img.src = url;
      img.alt = "証憑";
    })
    .catch(() => {
      wrap.replaceChildren(_lockPlaceholder("復号に失敗しました"));
    });
}


function _renderCard(c) {
  const col = document.createElement("div");
  col.className = "col-md-6 col-lg-4";
  const card = document.createElement("div");
  card.className = "card shadow-sm h-100";

  // header
  const header = document.createElement("div");
  header.className = "card-header d-flex justify-content-between align-items-center";
  const left = document.createElement("small");
  left.className = "text-muted";
  const right = document.createElement("span");
  if (c.attached) {
    left.textContent = _fmtYMD(c.entry_date || c.effective_date);
    if (c.entry_number != null) {
      right.appendChild(_badge("bg-secondary", "#" + c.entry_number));
    }
    if (c.overdue_days != null) {
      right.appendChild(document.createTextNode(" "));
      const od = _badge("bg-warning text-dark", "", "入力期限超過（" + c.overdue_days + "日）");
      od.innerHTML = '<i class="bi bi-clock-history"></i>';
      right.appendChild(od);
    }
  } else {
    left.textContent = _fmtYMDHM(c.uploaded_at);
    right.appendChild(_badge("bg-warning text-dark", "未紐付け"));
  }
  header.appendChild(left);
  header.appendChild(right);
  card.appendChild(header);

  // body (thumbnail + description + amount)
  const body = document.createElement("div");
  body.className = "card-body";
  const imgWrap = document.createElement("div");
  imgWrap.className = "text-center mb-2";
  body.appendChild(imgWrap);
  _renderCardImage(imgWrap, c);
  if (c.attached) {
    if (c.description) {
      const p = document.createElement("p");
      p.className = "mb-1";
      const strong = document.createElement("strong");
      strong.textContent = c.description;
      p.appendChild(strong);
      body.appendChild(p);
    }
    if (c.amount != null) {
      const amt = document.createElement("div");
      amt.className = "text-primary fw-bold";
      amt.textContent = _fmtYen(c.amount);
      body.appendChild(amt);
    }
  }
  card.appendChild(body);

  // footer (uploaded_at + hash/verify + journal link + delete)
  const footer = document.createElement("div");
  footer.className = "card-footer d-flex justify-content-between align-items-center";
  const fLeft = document.createElement("small");
  fLeft.className = "text-muted";
  fLeft.innerHTML = '<i class="bi bi-clock"></i> 保存: ';
  fLeft.appendChild(document.createTextNode(_fmtYMDHM(c.uploaded_at)));
  const fRight = document.createElement("span");

  if (c.has_hash) {
    const shield = document.createElement("span");
    shield.className = "text-success";
    shield.title = "SHA-256 記録済み";
    shield.innerHTML = '<i class="bi bi-shield-check"></i>';
    fRight.appendChild(shield);
    const vf = _postForm("/vouchers/" + encodeURIComponent(String(c.voucher_id)) + "/verify", null);
    const vb = document.createElement("button");
    vb.type = "submit";
    vb.className = "btn btn-outline-secondary btn-sm py-0 px-1";
    vb.title = "ハッシュ検証";
    vb.innerHTML = '<i class="bi bi-check2-circle"></i>';
    vf.appendChild(vb);
    fRight.appendChild(vf);
  } else {
    const noshield = document.createElement("span");
    noshield.className = "text-muted";
    noshield.title = "ハッシュ未記録";
    noshield.innerHTML = '<i class="bi bi-shield-x"></i>';
    fRight.appendChild(noshield);
  }

  if (c.attached && c.journal_entry_id != null) {
    const jl = document.createElement("a");
    jl.href = "/journal/" + encodeURIComponent(String(c.journal_entry_id)) + "/edit";
    jl.className = "text-decoration-none ms-1";
    jl.title = "仕訳を表示";
    jl.innerHTML = '<i class="bi bi-journal-text"></i>';
    fRight.appendChild(jl);
  }

  if (_canDelete) {
    const df = _postForm(
      "/vouchers/" + encodeURIComponent(String(c.voucher_id)) + "/delete",
      "この証憑を削除します。電帳法の訂正削除履歴はアプリケーションログにのみ残ります。削除しますか？",
    );
    const db_ = document.createElement("button");
    db_.type = "submit";
    db_.className = "btn btn-outline-danger btn-sm py-0 px-1";
    db_.title = "削除";
    db_.innerHTML = '<i class="bi bi-trash"></i>';
    df.appendChild(db_);
    fRight.appendChild(df);
  }

  footer.appendChild(fLeft);
  footer.appendChild(fRight);
  card.appendChild(footer);

  col.appendChild(card);
  return col;
}


function _renderCards(cards) {
  const grid = document.getElementById("vouchers-index-grid");
  const empty = document.getElementById("vouchers-index-empty");
  if (!grid) return;
  while (grid.firstChild) grid.removeChild(grid.firstChild);
  if (empty) empty.classList.toggle("d-none", cards.length !== 0);
  grid.classList.toggle("d-none", cards.length === 0);
  for (const c of cards) grid.appendChild(_renderCard(c));
}


function _currentFilters() {
  const g = (id) => {
    const el = document.getElementById(id);
    return el ? el.value : "";
  };
  return {
    dateFrom: g("v-date-from"),
    dateTo: g("v-date-to"),
    amountFrom: g("v-amount-from"),
    amountTo: g("v-amount-to"),
    search: g("v-search"),
  };
}


function _rerender() {
  _renderCards(filterVoucherCards(_allCards, _currentFilters()));
}


async function _run() {
  const paramsEl = document.getElementById("vouchers-index-params");
  if (!paramsEl) return;
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (_e) {
    _setStatus("証憑一覧の初期化に失敗しました。", "danger");
    return;
  }
  if (typeof params.user_id !== "number") {
    _setStatus("証憑一覧の初期化に失敗しました (params)。", "danger");
    return;
  }
  _canDelete = !!params.can_delete;
  _userId = params.user_id;
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  _csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  let voucherMeta = [];
  const metaEl = document.getElementById("vouchers-index-meta");
  try {
    if (metaEl) voucherMeta = JSON.parse(metaEl.textContent);
  } catch (_e) {
    _setStatus("証憑一覧の初期化に失敗しました (meta)。", "danger");
    return;
  }

  // 検索フォームの入力でクライアント側再描画。
  for (const id of ["v-date-from", "v-date-to", "v-amount-from", "v-amount-to", "v-search"]) {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", _rerender);
  }
  const resetBtn = document.getElementById("v-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", (e) => {
      e.preventDefault();
      for (const id of ["v-date-from", "v-date-to", "v-amount-from", "v-amount-to", "v-search"]) {
        const el = document.getElementById(id);
        if (el) el.value = "";
      }
      _rerender();
    });
  }

  // 紐付け仕訳の無い (= 未紐付け) 証憑のみでも、メタだけでカードは描画できる。
  // 紐付けありは仕訳復号で日付/摘要/金額を補完する。
  const buildAndRender = (entryMap) => {
    _allCards = buildVoucherCards(voucherMeta, entryMap);
    _rerender();
  };

  if (voucherMeta.length === 0) {
    _renderCards([]);
    return;
  }

  let client;
  try {
    const [{ SharedCryptoClient }, { fetchJournalsForYear }, voucherDl] =
      await Promise.all([
        import(getStaticRoot() + "js/crypto/shared-client.js"),
        import(getStaticRoot() + "js/crypto/journals_client.js"),
        import(getStaticRoot() + "js/crypto/voucher_download.js"),
      ]);

    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();

    // MK ロック / 代理閲覧では仕訳も暗号化証憑画像も復号できない。メタのみで
    // カードを描画し、暗号化証憑はロック表示にする (レガシー平文証憑は表示可)。
    if (!status.hasKey) {
      _setStatus(
        "暗号鍵 (MK) がロックされているため、仕訳の日付・摘要・金額と暗号化証憑画像は表示されません。保存日・伝票番号は表示されます (設定 → 暗号鍵管理 で解除)。",
        "warning",
      );
      buildAndRender(new Map());
      return;
    }
    if (params.is_audit_proxy) {
      _setStatus(
        "監査代理閲覧中です。オーナーの暗号化された仕訳・証憑画像は復号できないため、保存日・伝票番号のみ表示されます (E2EE アーキテクチャ仕様)。",
        "info",
      );
      buildAndRender(new Map());
      return;
    }
    _clearStatus();

    // E4 PR-C2: 暗号化証憑の復号表示を有効化 (本人 + MK 解錠時のみ)。
    // 画像復号にクライアントを使い続けるため、本パスでは finally でクローズ
    // しない (ページ生存中保持)。
    _client = client;
    _canDecrypt = true;
    _decryptImage = voucherDl.fetchAndDecryptVoucherImage;
    _sniffMime = voucherDl.sniffImageMime;
    // ページ離脱時にクライアント (SharedWorker port) を明示クローズし、保持中の
    // プレビュー blob URL も revoke する。
    if (typeof window !== "undefined") {
      window.addEventListener("beforeunload", () => {
        try { if (_client) _client.close(); } catch (_e) { /* ignore */ }
        if (_fullPreviewUrl) {
          try { URL.revokeObjectURL(_fullPreviewUrl); } catch (_e) { /* ignore */ }
        }
      }, { once: true });
    }

    // 紐付け仕訳の fiscal_year 群を取得・復号して entry_id → {…} を構築。
    const years = new Set();
    for (const v of voucherMeta) {
      if (v.journal_entry_id != null && v.fiscal_year != null) years.add(v.fiscal_year);
    }
    const entryMap = new Map();
    for (const y of years) {
      const journals = await fetchJournalsForYear({
        client, userId: params.user_id, fiscalYear: y,
      });
      for (const e of journals) {
        let amount = 0;
        for (const line of e.lines || []) amount += line.debit || 0;
        entryMap.set(e.id, {
          date: e.date || null,
          description: e.description || "",
          amount,
          entry_number: e.entry_number,
        });
      }
    }
    buildAndRender(entryMap);
  } catch (e) {
    _setStatus("証憑一覧の取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    // 暗号化証憑の復号にクライアントを使い続ける場合 (_canDecrypt) はクローズ
    // しない。それ以外 (ロック/代理/エラー) はクローズして資源を解放する。
    if (client && !_canDecrypt) {
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
