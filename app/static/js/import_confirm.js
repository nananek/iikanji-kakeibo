/**
 * 取込確認画面の共通JS
 * CSV / OFX / Web 取込で共有
 *
 * 使い方: initImportConfirm(parsedData, paymentAccountId)
 */
(function () {
  var _parsedData;
  var _paymentAccountId;

  /* ---------- ユーティリティ ---------- */

  function categoryNameById(id) {
    for (var i = 0; i < _acctSelectorData.length; i++) {
      var g = _acctSelectorData[i];
      for (var j = 0; j < g.accounts.length; j++) {
        if (g.accounts[j].id === id) return g.accounts[j].name;
      }
    }
    return '';
  }

  /* ---------- カウント更新 ---------- */

  function updateCount() {
    var count = 0, totalDep = 0, totalWd = 0;
    document.querySelectorAll('.row-check').forEach(function (cb) {
      if (cb.checked) {
        var idx = parseInt(cb.dataset.idx);
        count++;
        totalDep += _parsedData[idx].deposit || 0;
        totalWd += _parsedData[idx].withdrawal || 0;
      }
    });
    document.getElementById('importCount').textContent = count;
    document.getElementById('totalDeposit').textContent = '\u00a5' + totalDep.toLocaleString();
    document.getElementById('totalWithdrawal').textContent = '\u00a5' + totalWd.toLocaleString();
  }

  /* ---------- 全選択/全解除 ---------- */

  function toggleAll(checked) {
    document.querySelectorAll('.row-check').forEach(function (cb) { cb.checked = checked; });
    document.getElementById('checkAll').checked = checked;
    updateCount();
  }

  /* ---------- 費目ボタン初期化 ---------- */

  function setCategoryDisplay(btn, id, name) {
    var input = btn.parentElement.querySelector('.category-id');
    input.value = id;
    var span = btn.querySelector('.category-name');
    span.textContent = name;
    span.classList.remove('text-muted');
  }

  function initCategoryButtons() {
    document.querySelectorAll('.category-btn').forEach(function (btn) {
      var idx = btn.dataset.idx;
      var hiddenInput = document.querySelector('.category-id[data-idx="' + idx + '"]');
      var catId = parseInt(hiddenInput.value);
      if (catId) {
        var name = categoryNameById(catId);
        if (name) {
          var span = btn.querySelector('.category-name');
          span.textContent = name;
          span.classList.remove('text-muted');
        }
      }

      btn.addEventListener('click', function () {
        var thisBtn = this;
        var thisIdx = thisBtn.dataset.idx;
        var curId = document.querySelector('.category-id[data-idx="' + thisIdx + '"]').value;
        openAccountSelector(function (id, name) {
          setCategoryDisplay(thisBtn, id, name);
        }, { filter: 'category_transfer', excludeId: _paymentAccountId, activeTab: 'pl', currentId: curId });
      });
    });
  }

  /* ---------- 一括科目設定 ---------- */

  function bulkSetCategory() {
    var checked = document.querySelectorAll('.row-check:checked');
    if (checked.length === 0) {
      alert('行を選択してください。');
      return;
    }
    openAccountSelector(function (id, name) {
      checked.forEach(function (cb) {
        var idx = cb.dataset.idx;
        var btn = document.querySelector('.category-btn[data-idx="' + idx + '"]');
        if (btn) setCategoryDisplay(btn, id, name);
      });
      // 設定後に全チェック解除
      toggleAll(false);
    }, { filter: 'category_transfer', excludeId: _paymentAccountId, activeTab: 'pl' });
  }

  /* ---------- 摘要ソート ---------- */

  function initSort() {
    var th = document.getElementById('sortDescription');
    if (!th) return;

    var sortState = 0; // 0=元順, 1=昇順, 2=降順
    var tbody = document.querySelector('#confirmTable tbody');
    var originalOrder = Array.from(tbody.querySelectorAll('tr'));

    th.style.cursor = 'pointer';
    th.title = 'クリックでソート';

    th.addEventListener('click', function () {
      sortState = (sortState + 1) % 3;
      var rows = Array.from(tbody.querySelectorAll('tr'));

      if (sortState === 0) {
        // 元の順序に戻す
        originalOrder.forEach(function (tr) { tbody.appendChild(tr); });
      } else {
        rows.sort(function (a, b) {
          var idxA = parseInt(a.dataset.idx);
          var idxB = parseInt(b.dataset.idx);
          var descA = (_parsedData[idxA].description || '').toLowerCase();
          var descB = (_parsedData[idxB].description || '').toLowerCase();
          var cmp = descA < descB ? -1 : (descA > descB ? 1 : 0);
          return sortState === 2 ? -cmp : cmp;
        });
        rows.forEach(function (tr) { tbody.appendChild(tr); });
      }

      // アイコン更新
      var icon = th.querySelector('.sort-icon');
      if (icon) icon.textContent = sortState === 1 ? ' \u25B2' : (sortState === 2 ? ' \u25BC' : '');
    });
  }

  /* ---------- フォーム送信 ---------- */

  function initFormSubmit() {
    var form = document.getElementById('importForm');
    if (!form) return;

    form.addEventListener('submit', function () {
      var rows = [];
      document.querySelectorAll('.row-check').forEach(function (cb) {
        var idx = parseInt(cb.dataset.idx);
        var catInput = document.querySelector('.category-id[data-idx="' + idx + '"]');
        rows.push({
          enabled: cb.checked,
          date: _parsedData[idx].date,
          description: _parsedData[idx].description,
          deposit: _parsedData[idx].deposit,
          withdrawal: _parsedData[idx].withdrawal,
          category_id: catInput ? parseInt(catInput.value) : 0,
        });
      });
      document.getElementById('importRows').value = JSON.stringify(rows);
    });
  }

  /* ---------- 科目自動推定 ---------- */

  function suggestCategories() {
    var descriptions = [];
    for (var i = 0; i < _parsedData.length; i++) {
      if (_parsedData[i].description) {
        descriptions.push(_parsedData[i].description);
      }
    }
    if (descriptions.length === 0) return;

    var csrfToken = document.querySelector('meta[name="csrf-token"]');
    fetch('/journal/api/suggest-categories', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrfToken ? csrfToken.content : '',
      },
      body: JSON.stringify({
        descriptions: descriptions,
        payment_account_id: _paymentAccountId,
      }),
    })
      .then(function (res) { return res.json(); })
      .then(function (suggestions) {
        if (!suggestions || typeof suggestions !== 'object') return;
        document.querySelectorAll('.category-btn').forEach(function (btn) {
          var idx = parseInt(btn.dataset.idx);
          var desc = _parsedData[idx] && _parsedData[idx].description;
          if (!desc || !suggestions[desc]) return;

          // 推定結果で上書き（手動設定はこの時点ではまだ無い）
          setCategoryDisplay(btn, suggestions[desc].account_id, suggestions[desc].account_name);
        });
      })
      .catch(function () { /* 推定失敗は無視 */ });
  }

  /* ---------- 初期化 ---------- */

  window.initImportConfirm = function (parsedData, paymentAccountId) {
    _parsedData = parsedData;
    _paymentAccountId = paymentAccountId;

    initCategoryButtons();
    initSort();
    initDragSelect('#confirmTable', '.row-check', updateCount);
    initFormSubmit();

    document.querySelectorAll('.row-check').forEach(function (cb) {
      cb.addEventListener('change', updateCount);
    });

    updateCount();
    suggestCategories();
  };

  window.toggleAll = toggleAll;
  window.bulkSetCategory = bulkSetCategory;
  window.updateCount = updateCount;
  // parsedData への参照を外部から取得できるようにする（Web取込の日付一括変更用）
  window.getImportParsedData = function () { return _parsedData; };
})();
