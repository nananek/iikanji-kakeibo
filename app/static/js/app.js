// 金額入力フィールドでEnterキーを押した時のフォーム送信を防止（仕訳フォーム）
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('input[type="number"]').forEach(function(input) {
    input.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
      }
    });
  });
});

/**
 * Date を端末のローカルタイムゾーンで 'YYYY-MM-DD' にする。
 * `new Date().toISOString()` は UTC 基準で日付を返すため、日本時間の
 * 0:00〜8:59 に前日の日付になる。日付初期値には必ず本関数を使う。
 */
function formatLocalDate(d) {
  d = d || new Date();
  var mm = String(d.getMonth() + 1).padStart(2, '0');
  var dd = String(d.getDate()).padStart(2, '0');
  return d.getFullYear() + '-' + mm + '-' + dd;
}

/**
 * 証憑画像をモーダルでプレビュー表示（PWA対応）
 */
function openImagePreview(url) {
  var modal = document.getElementById('imagePreviewModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'imagePreviewModal';
    modal.className = 'modal fade';
    modal.tabIndex = -1;
    modal.innerHTML =
      '<div class="modal-dialog modal-lg modal-dialog-centered">' +
        '<div class="modal-content bg-dark border-0">' +
          '<div class="modal-header border-0 py-2">' +
            '<button type="button" id="imagePreviewNewTab" class="btn btn-sm btn-outline-light ms-auto me-2" title="別タブで開く">' +
              '<i class="bi bi-box-arrow-up-right"></i></button>' +
            '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>' +
          '</div>' +
          '<div class="modal-body text-center p-2">' +
            '<img id="imagePreviewImg" class="img-fluid" style="max-height:85vh;" alt="証憑">' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(modal);
  }
  document.getElementById('imagePreviewImg').src = url;
  document.getElementById('imagePreviewNewTab').onclick = function() {
    window.open(url, '_blank', 'noopener');
  };
  new bootstrap.Modal(modal).show();
}

/**
 * 証憑プレビューを描画する共通関数
 * @param {HTMLElement} container - 描画先の要素
 * @param {Array} vouchers - [{id, uploaded_at}, ...] 形式の証憑データ
 * @param {string} baseUrl - 証憑画像エンドポイントのベース（例: "/ai-journal/voucher/"）
 */
function renderVoucherPreview(container, vouchers, baseUrl) {
  if (!container || !vouchers || vouchers.length === 0) {
    if (container) container.classList.add('d-none');
    return;
  }
  baseUrl = baseUrl || '/ai-journal/voucher/';
  var html = '<div class="card shadow-sm mt-3 mb-0">' +
    '<div class="card-header"><i class="bi bi-file-image"></i> 証憑画像</div>' +
    '<div class="card-body text-center">';
  vouchers.forEach(function(v) {
    var url = baseUrl + v.id + '/image';
    var thumbUrl = url + '?size=thumb';
    html += '<a href="#" onclick="openImagePreview(\'' + url + '\');return false">' +
      '<img src="' + thumbUrl + '" class="img-fluid rounded" style="max-height:300px;cursor:pointer;" alt="証憑" loading="lazy">' +
      '</a> ';
  });
  html += '</div></div>';
  container.innerHTML = html;
  container.classList.remove('d-none');
}

/**
 * CSP 対応: インラインイベントハンドラ (onclick 等) の代替。
 * インライン属性は nonce を付けられず CSP で禁止されるため、外部スクリプト
 * から委譲処理する。属性:
 *   data-confirm        確認ダイアログ (form は submit 時、それ以外は click 時)
 *   data-action         グローバル関数を名前で呼ぶ
 *   data-action-change  change イベント版
 *   data-args           JSON 配列で引数を渡す
 *   data-self           要素自身を最後の引数に渡す
 *   data-prevent        preventDefault する
 */
(function () {
  function runAction(el, extra) {
    var name = el.getAttribute('data-action') || el.getAttribute('data-action-change');
    var fn = window[name];
    if (typeof fn !== 'function') return;
    var args = [];
    var raw = el.getAttribute('data-args');
    if (raw) { try { args = JSON.parse(raw); } catch (e) { args = []; } }
    if (el.hasAttribute('data-self')) args.push(el);
    if (extra !== undefined) args.push(extra);
    return fn.apply(null, args);
  }

  document.addEventListener('click', function (e) {
    if (!e.target || !e.target.closest) return;
    var c = e.target.closest('[data-confirm]');
    if (c && c.tagName !== 'FORM') {
      if (!window.confirm(c.getAttribute('data-confirm'))) {
        e.preventDefault(); e.stopImmediatePropagation(); return;
      }
    }
    var el = e.target.closest('[data-action]');
    if (el) {
      var r = runAction(el);
      if (r === false || el.hasAttribute('data-prevent')) e.preventDefault();
    }
  }, true);

  document.addEventListener('submit', function (e) {
    var msg = e.target && e.target.getAttribute && e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) {
      e.preventDefault(); e.stopImmediatePropagation();
    }
  }, true);

  document.addEventListener('change', function (e) {
    if (!e.target || !e.target.closest) return;
    var el = e.target.closest('[data-action-change]');
    if (el) runAction(el, e);
  }, true);

  // 汎用アクション (旧インライン JS の置換)
  window.submitFormById = function (el) {
    var id = el.getAttribute('data-target');
    var form = id ? document.getElementById(id) : el.form;
    if (form) form.submit();
  };
  window.toggleAllCheckboxes = function (el) {
    var sel = el.getAttribute('data-target');
    if (!sel) return;
    document.querySelectorAll(sel).forEach(function (cb) {
      if (!cb.disabled) cb.checked = !cb.checked;
    });
  };
  window.removeRowAndUpdate = function (el) {
    var tr = el.closest('tr');
    if (tr) tr.remove();
    if (typeof window.modalUpdateTotals === 'function') window.modalUpdateTotals();
  };
  window.showBookmarkletHint = function () {
    alert('このボタンをブックマークバーにドラッグしてください。');
    return false;
  };
  window.openPaymentSelector = function () {
    if (typeof window.openAccountSelector !== 'function') return;
    var el = document.getElementById('paymentAccountCode');
    window.openAccountSelector(window.onPaymentSelected, {
      filter: 'payment',
      currentCode: el ? el.value : '',
    });
  };
})();
