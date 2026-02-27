/**
 * テーブル行のドラッグ選択
 *
 * 使い方: initDragSelect(tableSelector, checkboxSelector, onChangeCallback)
 *
 * - チェックボックス列からドラッグすると範囲選択できる
 * - ドラッグ開始行〜現在行の範囲にdragValueを適用
 * - 範囲外に出た行はドラッグ前の状態に戻る
 * - タッチ対応（チェックボックス列以外のタップはスルー）
 */
(function () {
  window.initDragSelect = function (tableSelector, checkboxSelector, onChange) {
    var table = document.querySelector(tableSelector);
    if (!table) return;
    var tbody = table.querySelector('tbody');
    if (!tbody) return;

    var dragging = false;
    var dragValue = false;
    var startIdx = -1;
    var allRows = [];      // tbody内の全tr（順序）
    var savedStates = [];  // ドラッグ開始時の各チェック状態

    function onUpdate() {
      if (typeof onChange === 'function') onChange();
    }

    function getRowIndex(el) {
      var tr = el.closest('tr');
      if (!tr) return -1;
      return allRows.indexOf(tr);
    }

    function getCheckboxAt(idx) {
      if (idx < 0 || idx >= allRows.length) return null;
      return allRows[idx].querySelector(checkboxSelector);
    }

    function refreshRows() {
      allRows = Array.from(tbody.querySelectorAll('tr'));
    }

    function isInCheckboxCell(target, cb) {
      // タップ/クリック対象がチェックボックスを含む<td>内かを判定
      var td = target.closest('td');
      return td && td.contains(cb);
    }

    function applyRange(curIdx) {
      if (startIdx < 0 || curIdx < 0) return;
      var lo = Math.min(startIdx, curIdx);
      var hi = Math.max(startIdx, curIdx);

      for (var i = 0; i < allRows.length; i++) {
        var cb = getCheckboxAt(i);
        if (!cb) continue;
        if (i >= lo && i <= hi) {
          cb.checked = dragValue;
        } else {
          cb.checked = savedStates[i];
        }
      }
      onUpdate();
    }

    // チェックボックスの click イベントによる二重トグルを防止
    // (mousedown で手動設定した後、click のデフォルト動作で戻るのを防ぐ)
    tbody.addEventListener('click', function (e) {
      var target = e.target;
      if (target.matches && target.matches(checkboxSelector)) {
        e.preventDefault();
      }
    });

    // マウス
    tbody.addEventListener('mousedown', function (e) {
      refreshRows();
      var idx = getRowIndex(e.target);
      var cb = getCheckboxAt(idx);
      if (!cb) return;
      if (!isInCheckboxCell(e.target, cb)) return;

      e.preventDefault();
      dragging = true;
      startIdx = idx;

      // ドラッグ前の状態を保存
      savedStates = allRows.map(function (tr) {
        var c = tr.querySelector(checkboxSelector);
        return c ? c.checked : false;
      });

      dragValue = !cb.checked;
      cb.checked = dragValue;
      onUpdate();
      document.body.style.userSelect = 'none';
    });

    tbody.addEventListener('mouseover', function (e) {
      if (!dragging) return;
      var idx = getRowIndex(e.target);
      if (idx >= 0) applyRange(idx);
    });

    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      document.body.style.userSelect = '';
    });

    // タッチ
    tbody.addEventListener('touchstart', function (e) {
      refreshRows();
      var idx = getRowIndex(e.target);
      var cb = getCheckboxAt(idx);
      if (!cb) return;
      if (!isInCheckboxCell(e.target, cb)) return;

      e.preventDefault();
      dragging = true;
      startIdx = idx;

      savedStates = allRows.map(function (tr) {
        var c = tr.querySelector(checkboxSelector);
        return c ? c.checked : false;
      });

      dragValue = !cb.checked;
      cb.checked = dragValue;
      onUpdate();
    }, { passive: false });

    tbody.addEventListener('touchmove', function (e) {
      if (!dragging) return;
      e.preventDefault();
      var touch = e.touches[0];
      var el = document.elementFromPoint(touch.clientX, touch.clientY);
      if (!el) return;
      var idx = getRowIndex(el);
      if (idx >= 0) applyRange(idx);
    }, { passive: false });

    document.addEventListener('touchend', function () {
      if (!dragging) return;
      dragging = false;
    });
  };
})();
