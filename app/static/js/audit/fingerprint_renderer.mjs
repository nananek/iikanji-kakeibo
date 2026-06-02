// 監査アクセス管理画面 (settings/audit_grants) の TOFU fingerprint UI (E5 #112 / §14.4)。
//
// 各 grant の auditor について公開鍵を取得し、fingerprint を表示する。owner は
// 帯域外 (電話 / 対面) で auditor 本人に fingerprint を確認してから「固定」する。
// 以降サーバが返す公開鍵が固定値と異なれば警告する。MK は不要 (公開鍵のハッシュ
// と pinning のみ)。
//
// テンプレート側は各 grant に対し
//   <div data-auditor-fingerprint data-auditor-id="123" data-auditor-name="..."></div>
// を置く。本モジュールがその中身を描画する。

function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}

function _esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

async function _fetchPublicKey(auditorId) {
  const r = await fetch(`/api/v1/keypair/${auditorId}/public`, {
    credentials: "same-origin",
  });
  if (r.status === 404) return { ok: false, status: 404, publicKey: null };
  if (!r.ok) return { ok: false, status: r.status, publicKey: null };
  const data = await r.json();
  return { ok: true, status: 200, publicKey: data.public_key || null };
}

// fingerprint ラベルを <code> ブロックで装飾。グループ単位で折り返す。
function _labelHtml(label) {
  return `<code class="user-select-all">${_esc(label)}</code>`;
}

function _renderNoKey(el, name) {
  el.innerHTML = `
    <div class="alert alert-secondary small mb-0">
      <i class="bi bi-key"></i>
      <strong>${_esc(name)}</strong> はまだ暗号鍵を設定していません。
      監査者が設定 → 暗号鍵管理 で暗号鍵を有効にすると fingerprint を確認できます。
    </div>`;
}

function _renderError(el, name, status) {
  el.innerHTML = `
    <div class="alert alert-warning small mb-0">
      <i class="bi bi-exclamation-triangle"></i>
      <strong>${_esc(name)}</strong> の公開鍵を取得できませんでした (HTTP ${_esc(status)})。
    </div>`;
}

function _renderUnpinned(el, name, label, hashHex) {
  el.innerHTML = `
    <div class="alert alert-info small mb-0">
      <div class="mb-2">
        <i class="bi bi-shield-lock"></i>
        <strong>${_esc(name)}</strong> の公開鍵 fingerprint を確認してください。
      </div>
      <div class="mb-2">${_labelHtml(label)}</div>
      <p class="mb-2 text-muted">
        この fingerprint を <strong>監査者本人に電話または対面で読み上げてもらい</strong>、
        一致することを確認してから固定してください。メールやチャットなど盗聴され得る
        経路だけで確認しないでください。
      </p>
      <button type="button" class="btn btn-sm btn-primary"
              data-fp-action="pin"
              data-fp-hash="${_esc(hashHex)}">
        <i class="bi bi-pin-angle"></i> 本人に確認した — 固定する
      </button>
    </div>`;
}

function _renderMatch(el, name, label, pinnedAt) {
  const when = pinnedAt ? `（${_esc(pinnedAt.slice(0, 10))} 確認）` : "";
  el.innerHTML = `
    <div class="alert alert-success small mb-0">
      <div class="mb-1">
        <i class="bi bi-shield-check"></i>
        <strong>${_esc(name)}</strong> の公開鍵は確認済みです ${when}。
      </div>
      <div>${_labelHtml(label)}</div>
    </div>`;
}

function _renderMismatch(el, name, label, hashHex) {
  el.innerHTML = `
    <div class="alert alert-danger small mb-0">
      <div class="mb-2">
        <i class="bi bi-exclamation-octagon-fill"></i>
        <strong>${_esc(name)}</strong> の公開鍵が以前固定した値と<strong>変わっています</strong>。
        鍵がすり替えられた可能性があります。
      </div>
      <div class="mb-2">現在の fingerprint: ${_labelHtml(label)}</div>
      <p class="mb-2">
        監査者本人に連絡し、本当に鍵を作り直したのかを<strong>帯域外で</strong>確認して
        ください。意図した変更であることを確認できた場合のみ、新しい fingerprint を
        固定し直してください。
      </p>
      <button type="button" class="btn btn-sm btn-outline-danger"
              data-fp-action="pin"
              data-fp-hash="${_esc(hashHex)}">
        <i class="bi bi-pin-angle"></i> 本人に確認した — 新しい鍵で固定し直す
      </button>
    </div>`;
}

async function _renderOne(el, store, mod, b64decode) {
  const auditorId = Number(el.dataset.auditorId);
  const name = el.dataset.auditorName || `ユーザー#${auditorId}`;
  el.innerHTML =
    '<div class="text-muted small"><span class="spinner-border spinner-border-sm"></span> fingerprint を確認中…</div>';

  let res;
  try {
    res = await _fetchPublicKey(auditorId);
  } catch (e) {
    _renderError(el, name, "ネットワーク");
    return;
  }
  if (!res.ok) {
    _renderError(el, name, res.status);
    return;
  }
  if (!res.publicKey) {
    _renderNoKey(el, name);
    return;
  }

  const publicKeyRaw = b64decode(res.publicKey);
  const ev = await mod.evaluatePin(store, auditorId, publicKeyRaw);

  if (ev.status === "match") {
    _renderMatch(el, name, ev.label, ev.pinnedAt);
  } else if (ev.status === "mismatch") {
    _renderMismatch(el, name, ev.label, ev.hashHex);
  } else {
    _renderUnpinned(el, name, ev.label, ev.hashHex);
  }

  // 固定ボタンのハンドラ。確認済の意思表示としてユーザーが押したら pin する。
  const btn = el.querySelector('[data-fp-action="pin"]');
  if (btn) {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await mod.pinKey(store, auditorId, btn.dataset.fpHash, new Date().toISOString());
        await _renderOne(el, store, mod, b64decode);
      } catch (e) {
        btn.disabled = false;
        _renderError(el, name, "保存");
      }
    });
  }
}

/**
 * ページ内の全 fingerprint ウィジェットを初期化する。
 *
 * @param {number|string} ownerUserId  現在ログイン中の owner ユーザー ID。
 *   pinning ストアを owner ごとにスコープするため必須 (共有ブラウザ対策)。
 */
export async function initFingerprints(ownerUserId) {
  const widgets = Array.from(
    document.querySelectorAll("[data-auditor-fingerprint]"),
  );
  if (widgets.length === 0) return;

  const [mod, { b64decode }] = await Promise.all([
    import(getStaticRoot() + "js/crypto/key_pinning.js"),
    import(getStaticRoot() + "js/crypto/b64.js"),
  ]);
  let store;
  try {
    store = await mod.openPinStore(ownerUserId);
  } catch (e) {
    for (const el of widgets) {
      el.innerHTML =
        '<div class="alert alert-warning small mb-0">この環境では fingerprint の固定 (IndexedDB) が利用できません。</div>';
    }
    return;
  }

  // 並行に評価。各ウィジェットは独立しているので順不同で構わない。
  await Promise.all(widgets.map((el) => _renderOne(el, store, mod, b64decode)));
}
