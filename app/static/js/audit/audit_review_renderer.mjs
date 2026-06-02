// 監査者ダッシュボード — 受信スナップショットの復号・表示 (auditor 側, E5 #112 / §14.5/14.9)。
//
// auditor が owner から受信した最新ラウンドの AuditPackage を取得し、自分の MK で
// ラップ秘密鍵をアンラップ → HPKE 復号 (worker 内の hpkeOpen) して平文スナップショット
// を表示する。復号は SharedWorker 内で完結し、平文 X25519 秘密鍵はメインスレッドに
// 出ない (owner 送信 UI と対称)。
//
// PR-A は read-only 表示まで。修正案 / 差戻しの作成・送信 (sealAuditResponse →
// POST /api/v1/audit-responses) は PR-B で本モジュールに追記する。
//
// 純粋ロジック (latestRound / parseSnapshot / normalizeEntries) は DOM/crypto 非依存で
// export し node --test で単体検証する。

function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}

function getSharedWorkerUrl() {
  return globalThis.IIKANJI_SHARED_WORKER_URL || "/static/js/crypto/shared-worker.js";
}

// 復号済みコンテキスト (client / 最新 pkg / snapshot / cfg)。PR-B の修正案送信が
// getReviewCtx() で受け取る。平文スナップショットを globalThis に置かない (秘密鍵を
// worker 内に封じる設計ポリシーと整合)。
let _reviewCtx = null;

/** 直近に復号したスナップショットのコンテキストを返す (PR-B で利用)。 */
export function getReviewCtx() {
  return _reviewCtx;
}

// ---- 純粋ロジック (テスト対象) -----------------------------------------

/**
 * パッケージ群から最新ラウンド (最大 round_id) のパッケージを返す。無ければ null。
 * @param {Array<{round_id:number}>} packages
 * @returns {?Object}
 */
export function latestRound(packages) {
  let best = null;
  for (const p of packages || []) {
    if (!Number.isInteger(p.round_id)) continue;
    if (best === null || p.round_id > best.round_id) best = p;
  }
  return best;
}

/**
 * 復号した平文バイト列を JSON スナップショットへ復元する。
 * @param {Uint8Array} plaintextBytes
 * @returns {Object}
 */
export function parseSnapshot(plaintextBytes) {
  return JSON.parse(new TextDecoder().decode(plaintextBytes));
}

function _normLine(l) {
  return {
    account_code: l.account_code ?? null,
    // Lv2 (entries) は debit/credit、Lv3 (backup の jel body) は debit_amount/credit_amount。
    debit: l.debit ?? l.debit_amount ?? 0,
    credit: l.credit ?? l.credit_amount ?? 0,
  };
}

/**
 * Lv2 (entries: lines 入れ子) と Lv3 (journal_entries + journal_entry_lines を
 * journal_entry_id で結合) を共通形に正規化する。
 *   [{ id, date, description, lines: [{account_code, debit, credit}] }]
 * Lv1 (集計のみ) は仕訳本体を持たないので空配列。
 * @param {Object} snapshot
 * @returns {Array<Object>}
 */
export function normalizeEntries(snapshot) {
  if (!snapshot) return [];
  if (Array.isArray(snapshot.entries)) {
    return snapshot.entries.map((e) => ({
      id: e.id,
      date: e.date ?? "",
      description: e.description ?? "",
      lines: (e.lines || []).map(_normLine),
    }));
  }
  if (Array.isArray(snapshot.journal_entries)) {
    const linesByEntry = new Map();
    for (const l of snapshot.journal_entry_lines || []) {
      const k = l.journal_entry_id;
      if (!linesByEntry.has(k)) linesByEntry.set(k, []);
      linesByEntry.get(k).push(_normLine(l));
    }
    return snapshot.journal_entries.map((e) => ({
      id: e.id,
      date: e.date ?? "",
      description: e.description ?? "",
      lines: linesByEntry.get(e.id) || [],
    }));
  }
  return [];
}

// ---- DOM ヘルパー -------------------------------------------------------

function _esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function _status(msg, type = "info") {
  const el = document.getElementById("audit-review-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " small";
  el.classList.remove("d-none");
}

function _yen(n) {
  return Number(n || 0).toLocaleString("ja-JP");
}

function _csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

const LEVEL_LABELS = { 1: "Lv1: 集計のみ", 2: "Lv2: 税務科目", 3: "Lv3: 本人同等" };

// ---- データ取得 --------------------------------------------------------

async function _fetchAuditorPackages(grantId) {
  const r = await fetch("/api/v1/audit-packages?role=auditor", {
    credentials: "same-origin",
  });
  if (!r.ok) return [];
  const d = await r.json();
  return (d.audit_packages || []).filter((p) => p.audit_grant_id === grantId);
}

// ---- レンダリング ------------------------------------------------------

function _accountName(accountsMeta, code) {
  const m = (accountsMeta || {})[code];
  return m && m.name ? m.name : code;
}

function _renderTrialBalance(snapshot) {
  const rows = snapshot.trial_balance || [];
  if (rows.length === 0) return "";
  let dTotal = 0;
  let cTotal = 0;
  const body = rows
    .map((r) => {
      dTotal += r.debit || 0;
      cTotal += r.credit || 0;
      return `<tr>
        <td>${_esc(_accountName(snapshot.accounts_meta, r.account_code))}</td>
        <td class="text-end">${_yen(r.debit)}</td>
        <td class="text-end">${_yen(r.credit)}</td>
      </tr>`;
    })
    .join("");
  return `<h5 class="mt-4">試算表</h5>
    <div class="table-responsive">
    <table class="table table-sm">
      <thead><tr><th>科目</th><th class="text-end">借方</th><th class="text-end">貸方</th></tr></thead>
      <tbody>${body}</tbody>
      <tfoot><tr class="table-light fw-bold">
        <td>合計</td><td class="text-end">${_yen(dTotal)}</td><td class="text-end">${_yen(cTotal)}</td>
      </tr></tfoot>
    </table></div>`;
}

function _renderEntries(snapshot) {
  const entries = normalizeEntries(snapshot);
  if (entries.length === 0) return "";
  const rows = entries
    .map((e) => {
      const lines = e.lines
        .map(
          (l) =>
            `<div>${_esc(_accountName(snapshot.accounts_meta, l.account_code))}` +
            ` <span class="text-muted">借 ${_yen(l.debit)} / 貸 ${_yen(l.credit)}</span></div>`,
        )
        .join("");
      return `<tr>
        <td class="text-nowrap"><code>#${_esc(e.id)}</code></td>
        <td class="text-nowrap">${_esc(e.date)}</td>
        <td>${_esc(e.description)}</td>
        <td>${lines}</td>
      </tr>`;
    })
    .join("");
  return `<h5 class="mt-4">仕訳一覧 <span class="text-muted small">(${entries.length} 件)</span></h5>
    <div class="table-responsive">
    <table class="table table-sm align-middle">
      <thead><tr><th>ID</th><th>日付</th><th>摘要</th><th>明細</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>`;
}

// 証憑画像は owner が seal した平文に由来し、mime / base64 は敵対的 owner が
// 自由に細工できる。属性コンテキストへの文字列結合は (_esc が " を素通しするため)
// XSS になり得るので、DOM API で属性を設定して根本的に防ぐ。
function _appendVouchers(snapshot, container) {
  const vouchers = snapshot.vouchers || [];
  if (vouchers.length === 0) return;

  const h = document.createElement("h5");
  h.className = "mt-4";
  h.textContent = `証憑画像 (${vouchers.length} 件)`;
  container.appendChild(h);

  const row = document.createElement("div");
  row.className = "row g-2";
  for (const v of vouchers) {
    const col = document.createElement("div");
    if (v._imageError || !v.image_base64) {
      col.className = "col-auto";
      const box = document.createElement("div");
      box.className = "border rounded p-2 text-muted small";
      box.textContent = `証憑 #${v.voucher_id}（画像を復号できませんでした）`;
      col.appendChild(box);
    } else {
      col.className = "col-auto text-center";
      const img = document.createElement("img");
      // src への代入は属性パースを経ないため、細工された mime でも JS は実行されない。
      img.src = `data:${v.mime || "image/png"};base64,${v.image_base64}`;
      img.alt = `証憑 ${v.voucher_id}`;
      img.className = "border rounded";
      img.style.maxHeight = "160px";
      img.style.maxWidth = "160px";
      const cap = document.createElement("div");
      cap.className = "small text-muted";
      cap.textContent = `#${v.voucher_id}`;
      col.appendChild(img);
      col.appendChild(cap);
    }
    row.appendChild(col);
  }
  container.appendChild(row);
}

function _renderSnapshot(snapshot, pkg) {
  const body = document.getElementById("audit-snapshot-body");
  if (!body) return;
  const level = snapshot.level;
  const scope = level === 3 ? "全データ" : `${_esc(snapshot.fiscal_year)} 年度`;
  const header = `<div class="d-flex align-items-center gap-2 mb-2">
    <span class="badge bg-primary">${_esc(LEVEL_LABELS[level] || "不明")}</span>
    <span class="text-muted small">対象: ${scope} / 第 ${_esc(pkg.round_id)} 回</span>
  </div>`;
  // header / 試算表 / 仕訳一覧は全て要素テキストコンテキスト (_esc で安全)。
  body.innerHTML =
    header + _renderTrialBalance(snapshot) + _renderEntries(snapshot);
  // 証憑のみ属性に owner 制御値が入るので DOM API で追記する。
  _appendVouchers(snapshot, body);
}

// ---- メイン ------------------------------------------------------------

/**
 * 受信スナップショット表示を初期化する。
 * cfg = { static_root, grant_id, auditor_id, owner_id, permission_level }
 */
export async function initAuditReview(cfg) {
  _status("受信したスナップショットを確認しています…", "info");

  const packages = await _fetchAuditorPackages(cfg.grant_id);
  if (packages.length === 0) {
    _status("まだ受信したスナップショットはありません。", "secondary");
    return;
  }
  const pkg = latestRound(packages);

  // MK 解錠確認。
  const { SharedCryptoClient } = await import(
    getStaticRoot() + "js/crypto/shared-client.js"
  );
  const client = new SharedCryptoClient(getSharedWorkerUrl());
  const st = await client.status();
  if (!st.hasKey) {
    _status("暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。", "warning");
    return;
  }

  // 自分のラップ秘密鍵を取得。未設定なら鍵設定を促す。
  const { getKeyPair, privateKeyAAD } = await import(
    getStaticRoot() + "js/crypto/keypair.js"
  );
  const kp = await getKeyPair();
  if (!kp.encrypted_private_key) {
    _status("暗号鍵 (X25519) が未設定です。設定 → 暗号鍵管理 で有効にしてください。", "warning");
    return;
  }

  // HPKE open (worker 内)。
  const [{ packageAAD }, { b64decode }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/audit_hpke.js"),
    import(getStaticRoot() + "js/crypto/b64.js"),
  ]);
  try {
    const res = await client.hpkeOpen({
      encryptedPrivateKey: kp.encrypted_private_key,
      privIv: kp.private_key_iv,
      privAad: privateKeyAAD(cfg.auditor_id),
      enc: b64decode(pkg.ephemeral_pubkey),
      ciphertext: b64decode(pkg.ciphertext),
      aad: packageAAD(cfg.grant_id, pkg.round_id),
    });
    const snapshot = parseSnapshot(res.plaintext);
    _renderSnapshot(snapshot, pkg);
    _status(`第 ${pkg.round_id} 回スナップショットを復号しました。`, "success");
    // PR-B の修正案送信が getReviewCtx() で参照できるようモジュールスコープに保持。
    _reviewCtx = { client, pkg, snapshot, cfg };
    document.dispatchEvent(
      new CustomEvent("iikanji:audit-snapshot-ready", {
        detail: { packageId: pkg.id },
      }),
    );
  } catch (e) {
    _status(`スナップショットを復号できませんでした: ${e.message || e}`, "danger");
  }
}

// ---- PR-B: 修正案 / 差戻しの作成・送信 (コメント方式) -------------------
//
// auditor がスナップショットを見ながらコメント (全体 + 仕訳ごとの指摘) を書き、
// AuditResponse 平文 JSON を owner の公開鍵で HPKE seal して POST する。
// 送信契約 (§14.2 / 設計方針):
//   { v:1, response_type:"revision"|"rejection", summary?, comments?:[{entry_id, ref?, note}] }
// revision は summary か comments を 1 つ以上必須。rejection は差戻し/問題なしの合図で
// summary 任意。サーバは response_type のみ管理し中身は読めない。

/**
 * 入力から AuditResponse 平文 JSON を組み立てる純粋関数 (バリデーション込み)。
 * @param {Object} a
 * @param {string} a.responseType  "revision" | "rejection"
 * @param {string} [a.summary]
 * @param {Array<{entry_id:?number, ref?:string, note:string}>} [a.comments]
 * @returns {{v:number, response_type:string, summary?:string, comments?:Array}}
 */
export function buildResponseJson({ responseType, summary, comments }) {
  const type = responseType === "rejection" ? "rejection" : "revision";
  const cleanComments = [];
  for (const c of comments || []) {
    const note = (c.note || "").trim();
    if (!note) continue;
    const item = { entry_id: Number.isInteger(c.entry_id) ? c.entry_id : null, note };
    const ref = (c.ref || "").trim();
    if (ref) item.ref = ref;
    cleanComments.push(item);
  }
  const sum = (summary || "").trim();
  if (type === "revision" && cleanComments.length === 0 && !sum) {
    throw new Error("修正案には全体コメントか、仕訳ごとの指摘を 1 つ以上入力してください。");
  }
  const out = { v: 1, response_type: type };
  if (sum) out.summary = sum;
  if (cleanComments.length) out.comments = cleanComments;
  return out;
}

function _composeStatus(msg, type = "info") {
  const el = document.getElementById("audit-compose-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "mt-2 alert alert-" + type + " small";
  el.classList.remove("d-none");
}

async function _fetchPeerPublicKey(peerId) {
  const r = await fetch(`/api/v1/keypair/${peerId}/public`, {
    credentials: "same-origin",
  });
  if (!r.ok) return null;
  const d = await r.json();
  return d.public_key || null;
}

function _selectedResponseType() {
  const checked = document.querySelector('input[name="audit-response-type"]:checked');
  return checked ? checked.value : "revision";
}

// 仕訳ごとの指摘行の entry_id select を、復号したスナップショットの仕訳で埋める。
function _entryOptionLabel(e) {
  const date = e.date || "";
  const desc = e.description || "";
  return `#${e.id} ${date} ${desc}`.trim();
}

function _addCommentRow(entries) {
  const list = document.getElementById("audit-compose-comments");
  if (!list) return;
  const row = document.createElement("div");
  row.className = "row g-2 mb-2 align-items-center audit-comment-row";

  const selCol = document.createElement("div");
  selCol.className = "col-md-4";
  const sel = document.createElement("select");
  sel.className = "form-select form-select-sm audit-comment-entry";
  const optNone = document.createElement("option");
  optNone.value = "";
  optNone.textContent = "（全体について）";
  sel.appendChild(optNone);
  for (const e of entries) {
    const opt = document.createElement("option");
    opt.value = String(e.id);
    opt.textContent = _entryOptionLabel(e); // textContent なので XSS 安全
    opt.dataset.ref = `${e.date || ""} ${e.description || ""}`.trim();
    sel.appendChild(opt);
  }
  selCol.appendChild(sel);

  const noteCol = document.createElement("div");
  noteCol.className = "col-md-7";
  const note = document.createElement("input");
  note.type = "text";
  note.className = "form-control form-control-sm audit-comment-note";
  note.placeholder = "指摘内容（例: 貸方は普通預金では？）";
  noteCol.appendChild(note);

  const delCol = document.createElement("div");
  delCol.className = "col-md-1";
  const del = document.createElement("button");
  del.type = "button";
  del.className = "btn btn-sm btn-outline-danger";
  del.textContent = "×";
  del.addEventListener("click", () => row.remove());
  delCol.appendChild(del);

  row.appendChild(selCol);
  row.appendChild(noteCol);
  row.appendChild(delCol);
  list.appendChild(row);
}

function _collectComments() {
  const rows = Array.from(document.querySelectorAll(".audit-comment-row"));
  return rows.map((row) => {
    const sel = row.querySelector(".audit-comment-entry");
    const note = row.querySelector(".audit-comment-note");
    const entryId = sel && sel.value ? Number(sel.value) : null;
    const opt = sel && sel.selectedOptions[0];
    const ref = opt ? opt.dataset.ref || "" : "";
    return { entry_id: entryId, ref, note: note ? note.value : "" };
  });
}

function _resetCompose() {
  const summary = document.getElementById("audit-compose-summary");
  if (summary) summary.value = "";
  const list = document.getElementById("audit-compose-comments");
  if (list) list.innerHTML = "";
}

/**
 * 修正案作成フォームを初期化する。initAuditReview の後に呼ぶ (getReviewCtx が必要)。
 * 復号できていない / 受信パッケージが無い場合は何もしない。
 */
export async function initAuditCompose(cfg) {
  const ctx = getReviewCtx();
  const form = document.getElementById("audit-compose");
  if (!ctx || !form) return;
  form.classList.remove("d-none");

  const entries = normalizeEntries(ctx.snapshot);
  // Lv1 (集計のみ) は仕訳本体が無いので指摘行 UI は出さず、全体コメント/差戻しのみ。
  const addBtn = document.getElementById("audit-compose-add");
  const commentsWrap = document.getElementById("audit-compose-comments-wrap");
  if (entries.length === 0) {
    if (commentsWrap) commentsWrap.classList.add("d-none");
  } else if (addBtn) {
    addBtn.addEventListener("click", () => _addCommentRow(entries));
  }

  await _evaluateComposeGate(cfg, ctx);
  // owner 公開鍵 (peer=owner_id) の pin 成立でゲート再評価。
  document.addEventListener("iikanji:tofu-pinned", (e) => {
    if (!e.detail || Number(e.detail.auditorId) === Number(cfg.owner_id)) {
      _evaluateComposeGate(cfg, ctx);
    }
  });
}

async function _evaluateComposeGate(cfg, ctx) {
  const sendBtn = document.getElementById("audit-compose-send");
  if (!sendBtn) return;
  // 送信中はゲートを評価しない (await 中の再評価で送信ボタンを再有効化して二重 POST
  // させない, Finding 3)。
  if (ctx.sending) return;
  // どの早期 return パスでもボタンを無効のままにする。再評価が条件未達で抜けても
  // 古い ctx.ownerPubRaw のまま送信されないようにする (Finding 1)。
  sendBtn.disabled = true;

  const ownerPubB64 = await _fetchPeerPublicKey(cfg.owner_id);
  if (!ownerPubB64) {
    _composeStatus("相手 (owner) がまだ暗号鍵を設定していないため送信できません。", "secondary");
    return;
  }

  const keyPinning = await import(getStaticRoot() + "js/crypto/key_pinning.js");
  let store;
  try {
    store = await keyPinning.openPinStore(cfg.auditor_id);
  } catch (e) {
    _composeStatus("この環境では fingerprint の固定 (IndexedDB) が使えないため送信できません。", "warning");
    return;
  }
  const { b64decode } = await import(getStaticRoot() + "js/crypto/b64.js");
  const pubRaw = b64decode(ownerPubB64);
  const ev = await keyPinning.evaluatePin(store, cfg.owner_id, pubRaw, "OWNER");
  if (ev.status !== "match") {
    _composeStatus("送信する前に、上の相手の公開鍵 fingerprint を本人に確認して固定してください。", "info");
    return;
  }

  const st = await ctx.client.status();
  if (!st.hasKey) {
    _composeStatus("暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。", "warning");
    return;
  }

  // await をまたいだ間に送信が始まっていたら有効化しない (Finding 3)。
  if (ctx.sending) return;
  ctx.ownerPubRaw = pubRaw;
  _composeStatus("送信できます。", "success");
  sendBtn.disabled = false;
  if (!ctx.composeWired) {
    ctx.composeWired = true;
    sendBtn.addEventListener("click", () => _sendResponse(cfg, ctx));
  }
}

async function _sendResponse(cfg, ctx) {
  const sendBtn = document.getElementById("audit-compose-send");
  let payload;
  try {
    payload = buildResponseJson({
      responseType: _selectedResponseType(),
      summary: (document.getElementById("audit-compose-summary") || {}).value,
      comments: _collectComments(),
    });
  } catch (e) {
    _composeStatus(e.message || String(e), "warning");
    return;
  }

  // 送信中はゲート再評価がボタンを再有効化しないようにフラグを立てる (Finding 3)。
  ctx.sending = true;
  sendBtn.disabled = true;
  try {
    const [hpke, { b64encode }] = await Promise.all([
      import(getStaticRoot() + "js/crypto/audit_hpke.js"),
      import(getStaticRoot() + "js/crypto/b64.js"),
    ]);
    _composeStatus("暗号化して送信しています…", "info");
    const plaintext = new TextEncoder().encode(JSON.stringify(payload));
    const sealed = await hpke.sealAuditResponse(ctx.ownerPubRaw, plaintext, ctx.pkg.id);

    const resp = await fetch("/api/v1/audit-responses", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": _csrfToken(),
      },
      body: JSON.stringify({
        audit_package_id: ctx.pkg.id,
        response_type: payload.response_type,
        ephemeral_pubkey: b64encode(sealed.ephemeralPubkey),
        ciphertext: b64encode(sealed.ciphertext),
      }),
    });
    if (resp.status === 201) {
      const label = payload.response_type === "rejection" ? "差戻し" : "修正案";
      _composeStatus(`${label}を送信しました。`, "success");
      _resetCompose();
      ctx.sending = false;
      sendBtn.disabled = false;
      return;
    }
    if (resp.status === 403) {
      // 失効 / 期限切れは恒久失敗。再有効化せず無限リトライを防ぐ (Finding 2)。
      _composeStatus("送信が拒否されました。監査アクセスが失効、または送信期限を過ぎています。", "danger");
      ctx.sending = false;
      return;
    }
    if (resp.status === 400) {
      const e = await resp.json().catch(() => ({}));
      _composeStatus(`送信に失敗しました: ${e.error || resp.status}`, "danger");
    } else {
      _composeStatus(`送信に失敗しました (HTTP ${resp.status})。`, "danger");
    }
    // 400 / その他 HTTP は再試行余地があるので再有効化する。
    ctx.sending = false;
    sendBtn.disabled = false;
  } catch (e) {
    _composeStatus(`送信中にエラーが発生しました: ${e.message || e}`, "danger");
    ctx.sending = false;
    sendBtn.disabled = false;
  }
}
