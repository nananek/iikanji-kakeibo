/**
 * 取込確認画面の共通JS
 * CSV / OFX / Web 取込で共有
 *
 * 使い方: initImportConfirm(parsedData, paymentAccountId)
 */
(function () {
  var _parsedData;
  var _paymentAccountId;
  var _closedPeriods;
  var _restrictedBeforeYear;

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

  /* ---------- ソート ---------- */

  function initSort() {
    var tbody = document.querySelector('#confirmTable tbody');
    if (!tbody) return;
    var originalOrder = Array.from(tbody.querySelectorAll('tr'));
    var activeTh = null;
    var activeState = 0;

    function getValue(idx, key) {
      var row = _parsedData[idx];
      if (!row) return '';
      switch (key) {
        case 'row_num': return row.row_num || idx;
        case 'date': return row.date || '';
        case 'description': return (row.description || '').toLowerCase();
        case 'deposit': return row.deposit || 0;
        case 'withdrawal': return row.withdrawal || 0;
        default: return '';
      }
    }

    document.querySelectorAll('#confirmTable thead .sortable').forEach(function (th) {
      th.style.cursor = 'pointer';
      th.title = 'クリックでソート';

      th.addEventListener('click', function () {
        var key = th.dataset.sortKey;
        var isNumeric = (key === 'row_num' || key === 'deposit' || key === 'withdrawal');

        // 同じ列なら状態を進める、違う列なら昇順から
        if (activeTh === th) {
          activeState = (activeState + 1) % 3;
        } else {
          // 前の列のアイコンをリセット
          if (activeTh) {
            var prevIcon = activeTh.querySelector('.sort-icon');
            if (prevIcon) prevIcon.textContent = '';
          }
          activeTh = th;
          activeState = 1;
        }

        if (activeState === 0) {
          originalOrder.forEach(function (tr) { tbody.appendChild(tr); });
        } else {
          var rows = Array.from(tbody.querySelectorAll('tr'));
          rows.sort(function (a, b) {
            var va = getValue(parseInt(a.dataset.idx), key);
            var vb = getValue(parseInt(b.dataset.idx), key);
            var cmp;
            if (isNumeric) {
              cmp = va - vb;
            } else {
              cmp = va < vb ? -1 : (va > vb ? 1 : 0);
            }
            return activeState === 2 ? -cmp : cmp;
          });
          rows.forEach(function (tr) { tbody.appendChild(tr); });
        }

        var icon = th.querySelector('.sort-icon');
        if (icon) icon.textContent = activeState === 1 ? ' \u25B2' : (activeState === 2 ? ' \u25BC' : '');
      });
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

  /* ---------- 日付一括設定 ---------- */

  function bulkSetDate(selectedOnly) {
    var newDate = document.getElementById('bulkDate').value;
    if (!newDate) {
      alert('日付を入力してください。');
      return;
    }
    if (selectedOnly) {
      var checked = document.querySelectorAll('.row-check:checked');
      if (checked.length === 0) {
        alert('行を選択してください。');
        return;
      }
    }
    var appendOrig = document.getElementById('appendOriginalDate').checked;

    document.querySelectorAll('#confirmTable tbody tr').forEach(function (tr) {
      var idx = parseInt(tr.dataset.idx);
      var cb = tr.querySelector('.row-check');
      if (selectedOnly && (!cb || !cb.checked)) return;

      var origDate = _parsedData[idx].date;

      if (appendOrig && origDate) {
        var origFormatted = origDate.replace(/-/g, '/');
        var desc = _parsedData[idx].description;
        if (desc.indexOf('(' + origDate + ')') === -1 && desc.indexOf('（取引日:') === -1) {
          _parsedData[idx].description = desc + '（取引日: ' + origFormatted + '）';
          var descCell = tr.querySelector('.desc-cell');
          if (descCell) descCell.textContent = _parsedData[idx].description;
        }
      }

      _parsedData[idx].date = newDate;
      var dateCell = tr.querySelector('.date-cell');
      if (dateCell) dateCell.textContent = newDate;
      tr.classList.remove('table-warning');

      var status = getRowStatus(idx);
      if (status && !status.problem && (_parsedData[idx].deposit || _parsedData[idx].withdrawal)) {
        if (cb) cb.checked = true;
      }
      applyRowStatus(tr);
    });

    updateCount();
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

  /* ---------- 行ステータス判定・更新 ---------- */

  function getRowStatus(idx) {
    var row = _parsedData[idx];
    if (!row) return null;
    if (!row.date) return { cls: 'bg-warning text-dark', html: '日付なし' };
    if (!row.deposit && !row.withdrawal) return { cls: 'bg-secondary', html: '金額なし' };
    var year = parseInt(row.date.substring(0, 4));
    var month = parseInt(row.date.substring(5, 7));
    // 未開設年度チェック
    if (_restrictedBeforeYear && year < _restrictedBeforeYear) {
      return { cls: 'bg-warning text-dark', html: '<i class="bi bi-exclamation-triangle"></i> 年度未開設', problem: true };
    }
    // 確定済み期間チェック
    if (_closedPeriods && _closedPeriods[year] !== undefined && month <= _closedPeriods[year]) {
      return { cls: 'bg-danger', html: '<i class="bi bi-lock-fill"></i> 確定済み', problem: true };
    }
    return { cls: 'bg-success', html: 'OK' };
  }

  function applyRowStatus(tr) {
    var idx = parseInt(tr.dataset.idx);
    var status = getRowStatus(idx);
    if (!status) return;
    var statusTd = tr.querySelector('td:last-child');
    if (statusTd) {
      var badge = statusTd.querySelector('.badge');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'badge';
        statusTd.innerHTML = '';
        statusTd.appendChild(badge);
      }
      badge.className = 'badge ' + status.cls;
      badge.innerHTML = status.html;
    }
    // 問題行はチェックを外す（初期マーク時のみ）
    return status;
  }

  function markAllRowStatuses() {
    var hasRestricted = false;
    document.querySelectorAll('#confirmTable tbody tr').forEach(function (tr) {
      var status = applyRowStatus(tr);
      if (!status) return;
      if (status.problem) {
        var cb = tr.querySelector('.row-check');
        if (cb) cb.checked = false;
        if (status.html.indexOf('年度未開設') !== -1) hasRestricted = true;
      }
    });
    var bar = document.getElementById('oldYearBar');
    if (bar && hasRestricted) {
      bar.classList.remove('d-none');
    }
  }

  /* ---------- 日付クリック編集 ---------- */

  function initDateEdit() {
    document.querySelectorAll('#confirmTable .date-cell').forEach(function (td) {
      td.style.cursor = 'pointer';
      td.title = 'クリックで日付を変更';
      td.addEventListener('click', function () {
        if (td.querySelector('input')) return; // 既に編集中
        var idx = parseInt(td.dataset.idx);
        var currentDate = _parsedData[idx].date || '';
        var input = document.createElement('input');
        input.type = 'date';
        input.className = 'form-control form-control-sm';
        input.value = currentDate;
        td.textContent = '';
        td.appendChild(input);
        input.focus();

        function commit() {
          var newDate = input.value;
          if (newDate && newDate !== currentDate) {
            _parsedData[idx].date = newDate;
            td.textContent = newDate;
            // ステータス再評価
            var tr = td.closest('tr');
            applyRowStatus(tr);
            // 日付ありになったらチェックを入れる（問題なければ）
            var status = getRowStatus(idx);
            if (status && !status.problem) {
              var cb = tr.querySelector('.row-check');
              if (cb && !cb.checked && (_parsedData[idx].deposit || _parsedData[idx].withdrawal)) {
                cb.checked = true;
              }
            }
            updateCount();
          } else {
            td.textContent = currentDate || '(不明)';
          }
        }

        input.addEventListener('change', commit);
        input.addEventListener('blur', function () {
          // changeの後にblurが来る場合があるので少し遅延
          setTimeout(function () {
            if (td.querySelector('input')) commit();
          }, 100);
        });
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Escape') {
            td.textContent = currentDate || '(不明)';
          }
        });
      });
    });
  }

  /* ---------- 初期化 ---------- */

  window.initImportConfirm = function (parsedData, paymentAccountId, restrictedBeforeYear, closedPeriods) {
    _parsedData = parsedData;
    _paymentAccountId = paymentAccountId;
    _closedPeriods = closedPeriods || {};
    _restrictedBeforeYear = restrictedBeforeYear;

    initCategoryButtons();
    initSort();
    initDragSelect('#confirmTable', '.row-check', updateCount);
    initFormSubmit();
    initDateEdit();

    document.querySelectorAll('.row-check').forEach(function (cb) {
      cb.addEventListener('change', updateCount);
    });

    markAllRowStatuses();
    updateCount();
    suggestCategories();
  };

  window.toggleAll = toggleAll;
  window.bulkSetCategory = bulkSetCategory;
  window.bulkSetDate = bulkSetDate;
  window.updateCount = updateCount;
  window.getImportParsedData = function () { return _parsedData; };
})();
