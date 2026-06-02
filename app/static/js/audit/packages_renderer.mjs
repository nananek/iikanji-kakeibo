// 監査スナップショット送信 UI (owner 側, E5 #112 / §14.5/14.6/14.7)。
//
// owner がこの場でスナップショットを生成 → 監査者の公開鍵で HPKE seal →
// POST /api/v1/audit-packages する。HPKE seal は秘密鍵不要なのでメインスレッドで
// 実行する (audit_hpke.sealAuditPackage)。送信は次の 3 条件をすべて満たす時のみ:
//   1. 監査者が公開鍵を設定済み
//   2. その公開鍵 fingerprint を owner が帯域外確認・固定済み (TOFU, PR-A)
//   3. owner の MK が解錠済み (スナップショットの復号生成に必要)
//
// 純粋ロジック (computeNextRound / unacknowledgedForGrant) は IndexedDB/DOM 非依存で
// export し node --test で単体検証する。

function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}

function getSharedWorkerUrl() {
  return globalThis.IIKANJI_SHARED_WORKER_URL || "/static/js/crypto/shared-worker.js";
}

function _csrfToken() {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
}

// ---- 純粋ロジック (テスト対象) -----------------------------------------

/**
 * 既存パッケージ群から次の round_id を決める (最大 + 1、無ければ 1)。
 * @param {Array<{round_id:number}>} packages
 * @returns {number}
 */
export function computeNextRound(packages) {
  let max = 0;
  for (const p of packages || []) {
    if (Number.isInteger(p.round_id) && p.round_id > max) max = p.round_id;
  }
  return max + 1;
}

/**
 * この grant のパッケージに対する、owner 未確認 (owner_acknowledged_at == null) の
 * 修正案/差戻し response を返す (§14.7 の未処理警告用)。
 * @param {Array<{audit_package_id:number, owner_acknowledged_at:?string}>} responses
 * @param {Array<number>} packageIds  この grant のパッケージ id 群
 * @returns {Array}
 */
export function unacknowledgedForGrant(responses, packageIds) {
  const ids = new Set(packageIds);
  return (responses || []).filter(
    (r) => ids.has(r.audit_package_id) && !r.owner_acknowledged_at,
  );
}

// ---- DOM ヘルパー -------------------------------------------------------

function _esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function _status(msg, type = "info") {
  const el = document.getElementById("audit-send-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "mt-3 alert alert-" + type + " small";
  el.classList.remove("d-none");
}

// ---- データ取得 --------------------------------------------------------

async function _fetchAuditorPublicKey(auditorId) {
  const r = await fetch(`/api/v1/keypair/${auditorId}/public`, {
    credentials: "same-origin",
  });
  if (!r.ok) return null;
  const d = await r.json();
  return d.public_key || null;
}

async function _fetchOwnerPackages(grantId) {
  const r = await fetch("/api/v1/audit-packages?role=owner", {
    credentials: "same-origin",
  });
  if (!r.ok) return [];
  const d = await r.json();
  return (d.audit_packages || []).filter((p) => p.audit_grant_id === grantId);
}

async function _fetchOwnerResponses() {
  const r = await fetch("/api/v1/audit-responses", { credentials: "same-origin" });
  if (!r.ok) return [];
  const d = await r.json();
  return d.audit_responses || [];
}

// ---- レンダリング ------------------------------------------------------

function _renderPackages(packages) {
  const el = document.getElementById("audit-packages-list");
  if (!el) return;
  if (packages.length === 0) {
    el.innerHTML = '<span class="text-muted">まだ送信していません。</span>';
    return;
  }
  const rows = packages
    .slice()
    .sort((a, b) => b.round_id - a.round_id)
    .map((p) => {
      const created = p.created_at ? _esc(p.created_at.slice(0, 10)) : "";
      const accepted = p.owner_accepted_at
        ? '<span class="badge bg-success">採用確定</span>'
        : "";
      return `<tr>
        <td>第 ${_esc(p.round_id)} 回</td>
        <td>${created}</td>
        <td>${accepted}</td>
      </tr>`;
    })
    .join("");
  el.innerHTML = `<table class="table table-sm mb-0">
    <thead><tr><th>ラウンド</th><th>送信日</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function _renderPending(pending) {
  const el = document.getElementById("audit-pending-responses");
  if (!el) return;
  if (pending.length === 0) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = `<div class="alert alert-warning">
    <i class="bi bi-exclamation-triangle"></i>
    監査者から未処理の修正案 / 差戻しが ${_esc(pending.length)} 件あります。
    新しいスナップショットを送る前に内容を確認することを推奨します。
  </div>`;
}

// ---- メイン ------------------------------------------------------------

export async function initAuditPackages(cfg) {
  const [keyPinning, snapshot, hpke, b64] = await Promise.all([
    import(getStaticRoot() + "js/crypto/key_pinning.js"),
    import(getStaticRoot() + "js/crypto/audit_snapshot.js"),
    import(getStaticRoot() + "js/crypto/audit_hpke.js"),
    import(getStaticRoot() + "js/crypto/b64.js"),
  ]);
  const deps = { keyPinning, snapshot, hpke, b64 };

  // 既存パッケージ + 未処理 response を並行取得して描画。
  const [packages, responses] = await Promise.all([
    _fetchOwnerPackages(cfg.grant_id),
    _fetchOwnerResponses(),
  ]);
  _renderPackages(packages);
  const pending = unacknowledgedForGrant(responses, packages.map((p) => p.id));
  _renderPending(pending);

  // ctx は安定参照。連続送信で computeNextRound が最新 packages を見るよう
  // _send が ctx.packages を更新する。wired で送信ボタンの二重配線を防ぐ。
  const ctx = { ...deps, packages, pending, client: null, pubKeyRaw: null, wired: false };

  await _evaluateGate(cfg, ctx);
  // fingerprint widget (同一ページ) で pin が成立したらゲートを再評価する。
  document.addEventListener("iikanji:tofu-pinned", (e) => {
    if (!e.detail || Number(e.detail.auditorId) === Number(cfg.auditor_id)) {
      _evaluateGate(cfg, ctx);
    }
  });
}

/**
 * 送信ゲートを評価し、条件 (公開鍵あり + pin 一致 + MK 解錠 + 年度あり) を満たせば
 * 送信ボタンを有効化する。pin 後の再評価でも呼ばれるため冪等に書く。
 */
async function _evaluateGate(cfg, ctx) {
  const sendBtn = document.getElementById("audit-send-btn");
  if (!sendBtn) return;

  const auditorPubKeyB64 = await _fetchAuditorPublicKey(cfg.auditor_id);
  if (!auditorPubKeyB64) {
    _status("監査者がまだ暗号鍵を設定していません。設定されると送信できます。", "secondary");
    return;
  }

  let store;
  try {
    store = await ctx.keyPinning.openPinStore(cfg.owner_id);
  } catch (e) {
    _status("この環境では fingerprint の固定 (IndexedDB) が使えないため送信できません。", "warning");
    return;
  }
  const pubKeyRaw = ctx.b64.b64decode(auditorPubKeyB64);
  const ev = await ctx.keyPinning.evaluatePin(store, cfg.auditor_id, pubKeyRaw);
  if (ev.status !== "match") {
    _status("送信する前に、上の監査者の公開鍵 fingerprint を本人に確認して固定してください。", "info");
    return;
  }

  // MK 解錠チェック。client は一度だけ生成する。
  if (!ctx.client) {
    const { SharedCryptoClient } = await import(
      getStaticRoot() + "js/crypto/shared-client.js"
    );
    ctx.client = new SharedCryptoClient(getSharedWorkerUrl());
  }
  const st = await ctx.client.status();
  if (!st.hasKey) {
    _status("暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。", "warning");
    return;
  }

  if (cfg.permission_level !== 3 && (cfg.fiscal_years || []).length === 0) {
    _status("仕訳のある年度がないため送信できません。", "warning");
    return;
  }

  ctx.pubKeyRaw = pubKeyRaw;
  _status("送信できます。", "success");
  sendBtn.disabled = false;
  if (!ctx.wired) {
    ctx.wired = true;
    sendBtn.addEventListener("click", () => _send(cfg, ctx));
  }
}

async function _buildSnapshot(cfg, client, snapshot) {
  const accountsMeta = cfg.accounts_meta || {};
  const userId = cfg.owner_id;
  if (cfg.permission_level === 3) {
    return snapshot.buildSnapshotLv3({ client, userId, accountsMeta });
  }
  const sel = document.getElementById("audit-fiscal-year");
  const fiscalYear = Number(sel && sel.value);
  if (!Number.isInteger(fiscalYear)) {
    throw new Error("対象年度を選択してください");
  }
  const args = { client, userId, fiscalYear, accountsMeta };
  return cfg.permission_level === 2
    ? snapshot.buildSnapshotLv2(args)
    : snapshot.buildSnapshotLv1(args);
}

async function _send(cfg, ctx) {
  const { client, snapshot, hpke, b64, pubKeyRaw, packages, pending } = ctx;
  const sendBtn = document.getElementById("audit-send-btn");

  if (pending.length > 0) {
    const ok = window.confirm(
      `監査者から未処理の修正案 / 差戻しが ${pending.length} 件あります。` +
        "それでも新しいスナップショットを送信しますか？",
    );
    if (!ok) return;
  }

  sendBtn.disabled = true;
  try {
    _status("スナップショットを生成しています…", "info");
    const snap = await _buildSnapshot(cfg, client, snapshot);
    const plaintext = new TextEncoder().encode(JSON.stringify(snap));

    _status("監査者の公開鍵で暗号化しています…", "info");
    const nextRound = computeNextRound(packages);
    const sealed = await hpke.sealAuditPackage(
      pubKeyRaw, plaintext, cfg.grant_id, nextRound,
    );

    _status("送信しています…", "info");
    const resp = await fetch("/api/v1/audit-packages", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": _csrfToken(),
      },
      body: JSON.stringify({
        audit_grant_id: cfg.grant_id,
        round_id: nextRound,
        permission_level: cfg.permission_level,
        ephemeral_pubkey: b64.b64encode(sealed.ephemeralPubkey),
        ciphertext: b64.b64encode(sealed.ciphertext),
        snapshot_hash: b64.b64encode(sealed.snapshotHash),
      }),
    });
    if (resp.status === 201) {
      _status(`第 ${nextRound} 回スナップショットを送信しました。`, "success");
      const fresh = await _fetchOwnerPackages(cfg.grant_id);
      _renderPackages(fresh);
      // packages を更新して次回 round を進める。
      ctx.packages = fresh;
      sendBtn.disabled = false;
      return;
    }
    if (resp.status === 409) {
      _status("このラウンドは既に送信済みです。ページを再読み込みしてください。", "warning");
    } else if (resp.status === 400) {
      const e = await resp.json().catch(() => ({}));
      const tooLarge = (e.error || "").includes("too large");
      _status(
        tooLarge
          ? "スナップショットが大きすぎます。証憑画像の多い年度は分割を検討してください。"
          : `送信に失敗しました: ${e.error || resp.status}`,
        "danger",
      );
    } else {
      _status(`送信に失敗しました (HTTP ${resp.status})。`, "danger");
    }
    sendBtn.disabled = false;
  } catch (e) {
    _status(`送信中にエラーが発生しました: ${e.message || e}`, "danger");
    sendBtn.disabled = false;
  }
}
