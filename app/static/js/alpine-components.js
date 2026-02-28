/**
 * Alpine.js 共通コンポーネント定義
 * alpine:init イベントで Alpine 起動前に登録される
 */
document.addEventListener('alpine:init', function() {

  // CSRF トークンストア（fetch呼び出し用）
  Alpine.store('csrf', {
    get token() {
      return document.querySelector('meta[name="csrf-token"]').content;
    }
  });

  /**
   * 月次確定チェック + 計上期間による日付自動設定
   *
   * 使い方:
   *   <div x-data="fiscalPeriodChecker({
   *     closedPeriods: { ... },
   *     restrictedBefore: 2024,
   *     dateValue: '2026-01-15',
   *     fiscalPeriod: ''        // optional: 計上期間セレクト連動
   *   })">
   */
  Alpine.data('fiscalPeriodChecker', function(config) {
    return {
      closedPeriods: config.closedPeriods || {},
      restrictedBefore: config.restrictedBefore,
      dateValue: config.dateValue || '',
      fiscalPeriod: config.fiscalPeriod != null ? String(config.fiscalPeriod) : '',
      dateReadOnly: false,
      warningMessage: '',
      isBlocked: false,

      init: function() {
        this.onFiscalPeriodChange();
      },

      checkDate: function() {
        if (!this.dateValue) {
          this.warningMessage = '';
          this.isBlocked = false;
          return;
        }
        var year = parseInt(this.dateValue.substring(0, 4));
        var month = parseInt(this.dateValue.substring(5, 7));
        var period = this.fiscalPeriod !== '' ? parseInt(this.fiscalPeriod) : month;
        var msg = null;

        if (this.restrictedBefore && year < this.restrictedBefore && this.closedPeriods[year] === undefined) {
          msg = year + '年度は開設されていません。';
        }
        var closed = this.closedPeriods[year];
        if (!msg && closed !== undefined && period <= closed) {
          var label = period === 0 ? '期首' : period <= 12 ? period + '月' : '決算整理' + (period - 12);
          msg = year + '年 ' + label + ' は月次確定済みです。この日付では保存できません。';
        }

        this.warningMessage = msg || '';
        this.isBlocked = !!msg;
      },

      onFiscalPeriodChange: function() {
        if (!this.dateValue) { this.checkDate(); return; }
        var year = this.dateValue.substring(0, 4);
        var fp = this.fiscalPeriod;
        if (fp === '0') {
          this.dateValue = year + '-01-01';
          this.dateReadOnly = true;
        } else if (fp === '13' || fp === '14' || fp === '15') {
          this.dateValue = year + '-12-31';
          this.dateReadOnly = true;
        } else {
          this.dateReadOnly = false;
        }
        this.checkDate();
      }
    };
  });

  /**
   * 仕訳帳一覧: 一括選択 + ドラッグ選択連携
   *
   * 使い方:
   *   <form x-data="bulkSelect" ...>
   */
  Alpine.data('bulkSelect', function() {
    return {
      selectedCount: 0,
      allSelected: false,

      init: function() {
        this.updateCount();
        var self = this;
        document.addEventListener('htmx:afterSwap', function() { self.updateCount(); });
      },

      updateCount: function() {
        var checked = this.$el.querySelectorAll('.entry-cb:checked').length;
        var total = this.$el.querySelectorAll('.entry-cb').length;
        this.selectedCount = checked;
        this.allSelected = checked === total && total > 0;
      },

      toggleAll: function() {
        var cbs = this.$el.querySelectorAll('.entry-cb');
        var val = this.allSelected;
        cbs.forEach(function(cb) { cb.checked = val; });
        this.updateCount();
      },

      confirmBulkDelete: function() {
        var withVoucher = this.$el.querySelectorAll('.entry-cb:checked[data-has-voucher="true"]').length;
        var msg = withVoucher > 0
          ? withVoucher + '件の仕訳に証憑が紐づいています。削除すると証憑が未紐付けになります。削除しますか？'
          : '選択した仕訳を削除しますか？この操作は取り消せません。';
        return confirm(msg);
      }
    };
  });

  /**
   * 勘定科目管理: 科目追加・編集モーダル
   *
   * 使い方:
   *   <div x-data="accountEditor({
   *     expenseTypeId: 5, revenueTypeId: 4,
   *     defaultTypeId: 1, accountsByType: { ... }
   *   })">
   */
  Alpine.data('accountEditor', function(config) {
    return {
      expenseTypeId: config.expenseTypeId,
      revenueTypeId: config.revenueTypeId,
      accountsByType: config.accountsByType,
      editingId: null,
      editingTypeId: null,
      wasActive: true,
      code: '',
      name: '',
      accountTypeId: String(config.defaultTypeId),
      description: '',
      taxCategory: '',
      costType: '',
      isActive: true,
      codeReadOnly: false,
      typeDisabled: false,
      activeDisabled: false,
      errorMessage: '',
      saving: false,
      modalTitle: '科目を追加',
      modal: null,
      showDeactivate: false,
      deactivateLoading: false,
      balanceLabel: '',
      hasBalance: false,
      transferToId: '',
      transferCandidates: [],

      get showCostType() {
        var t = parseInt(this.accountTypeId);
        return t === this.expenseTypeId || t === this.revenueTypeId;
      },
      get costLabel() {
        return parseInt(this.accountTypeId) === this.revenueTypeId ? '収入区分' : '費用区分';
      },

      getModal: function() {
        if (!this.modal) this.modal = new bootstrap.Modal(document.getElementById('accountModal'));
        return this.modal;
      },

      resetForm: function() {
        this.editingId = null;
        this.editingTypeId = null;
        this.wasActive = true;
        this.code = '';
        this.name = '';
        this.accountTypeId = String(config.defaultTypeId);
        this.description = '';
        this.taxCategory = '';
        this.costType = '';
        this.isActive = true;
        this.codeReadOnly = false;
        this.typeDisabled = false;
        this.activeDisabled = false;
        this.errorMessage = '';
        this.saving = false;
        this.showDeactivate = false;
        this.transferToId = '';
      },

      open: function(accountId, copy) {
        this.resetForm();
        if (accountId) {
          var self = this;
          var url = '/accounts/api/' + accountId;
          if (copy) url += '?copy=1';
          fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
              self.editingId = data.id;
              self.editingTypeId = data.account_type_id;
              self.wasActive = data.is_active;
              self.modalTitle = copy ? 'コピーして追加' : '科目を編集';
              self.code = data.code;
              self.codeReadOnly = !copy && data.is_system;
              self.name = data.name;
              self.accountTypeId = String(data.account_type_id);
              self.typeDisabled = !copy && data.is_system;
              self.description = data.description;
              self.taxCategory = data.tax_category;
              self.costType = data.cost_type;
              self.isActive = data.is_active;
              self.activeDisabled = !copy && !!data.system_role;
              self.getModal().show();
            });
        } else {
          this.modalTitle = '科目を追加';
          this.getModal().show();
        }
      },

      onActiveChange: function() {
        if (!this.isActive && this.editingId && this.wasActive) {
          this.showDeactivate = true;
          this.deactivateLoading = true;
          this.hasBalance = false;
          var self = this;
          fetch('/accounts/api/' + this.editingId + '/balance')
            .then(function(r) { return r.json(); })
            .then(function(data) {
              self.deactivateLoading = false;
              if (data.balance !== 0) {
                self.hasBalance = true;
                self.balanceLabel = '\u00a5' + Math.abs(data.balance).toLocaleString() +
                  (data.balance < 0 ? '（貸方残）' : '（借方残）');
                var candidates = self.accountsByType[self.editingTypeId] || [];
                self.transferCandidates = candidates.filter(function(a) {
                  return a.id !== self.editingId;
                });
              }
            });
        } else {
          this.showDeactivate = false;
        }
      },

      save: function() {
        this.errorMessage = '';
        var payload = {
          code: this.code,
          name: this.name,
          account_type_id: parseInt(this.accountTypeId),
          description: this.description,
          tax_category: this.taxCategory,
          cost_type: this.costType,
          is_active: this.isActive,
        };
        if (!this.isActive && this.editingId && this.wasActive && this.transferToId) {
          payload.transfer_to_account_id = parseInt(this.transferToId);
        }
        var url = this.editingId ? ('/accounts/api/' + this.editingId) : '/accounts/api/new';
        this.saving = true;
        var self = this;
        fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content,
          },
          body: JSON.stringify(payload),
        })
        .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
        .then(function(res) {
          self.saving = false;
          if (!res.ok) {
            self.errorMessage = res.data.error || 'エラーが発生しました。';
            return;
          }
          location.reload();
        })
        .catch(function() {
          self.saving = false;
          self.errorMessage = '通信エラーが発生しました。';
        });
      }
    };
  });

  /**
   * 仕訳明細行: 動的行追加・削除、貸借合計、科目選択連携
   *
   * 使い方:
   *   <form x-data="journalLines({ lines: [...], fullName: false })"
   *         @submit="serializeLines()">
   *     <input type="hidden" name="lines_json" x-ref="linesJson">
   *     <template x-for="(line, index) in lines" :key="line._key"> ... </template>
   */
  Alpine.data('journalLines', function(config) {
    var _keyCounter = 0;

    return {
      lines: [],
      get totalDebit() {
        var sum = 0;
        for (var i = 0; i < this.lines.length; i++) sum += parseInt(this.lines[i].debit_amount) || 0;
        return sum;
      },
      get totalCredit() {
        var sum = 0;
        for (var i = 0; i < this.lines.length; i++) sum += parseInt(this.lines[i].credit_amount) || 0;
        return sum;
      },
      get isUnbalanced() {
        return this.totalDebit !== this.totalCredit && (this.totalDebit > 0 || this.totalCredit > 0);
      },

      init: function() {
        var initLines = config.lines || [];
        if (initLines.length > 0) {
          for (var i = 0; i < initLines.length; i++) this.addLine(initLines[i]);
        } else {
          this.addLine();
          this.addLine();
        }
      },

      addLine: function(data) {
        data = data || {};
        var name = '';
        if (data.account_id) {
          name = typeof _acctNameById === 'function'
            ? _acctNameById(data.account_id, config.fullName) : '';
          if (data.is_proprietor && !name) name = '事業主';
        }
        this.lines.push({
          _key: ++_keyCounter,
          account_id: data.account_id || '',
          account_name: name,
          debit_amount: data.debit_amount || 0,
          credit_amount: data.credit_amount || 0,
          description: data.description || '',
          is_proprietor: data.is_proprietor || false,
        });
      },

      removeLine: function(index) {
        this.lines.splice(index, 1);
      },

      selectAccount: function(index, filter) {
        var line = this.lines[index];
        openAccountSelector(function(id, name) {
          line.account_id = id;
          line.account_name = name;
        }, {filter: filter || 'all', currentId: line.account_id});
      },

      serializeLines: function() {
        var result = [];
        for (var i = 0; i < this.lines.length; i++) {
          var line = this.lines[i];
          var debit = parseInt(line.debit_amount) || 0;
          var credit = parseInt(line.credit_amount) || 0;
          if (line.account_id && (debit > 0 || credit > 0)) {
            result.push({
              account_id: line.account_id,
              debit_amount: debit,
              credit_amount: credit,
              description: line.description
            });
          }
        }
        this.$refs.linesJson.value = JSON.stringify(result);
      }
    };
  });

  /**
   * 科目選択モーダル
   *
   * 使い方:
   *   <div id="accountSelectorModal" x-data="accountSelector(_acctSelectorData)"
   *        @open-selector="open($event.detail)">
   */
  Alpine.data('accountSelector', function(allGroups) {
    var bsCodes = ['asset', 'liability', 'equity'];
    var plCodes = ['expense', 'revenue'];
    var filterMap = {
      all: ['asset', 'liability', 'equity', 'expense', 'revenue'],
      payment: ['asset', 'liability'],
      category: ['expense', 'revenue'],
      category_transfer: ['asset', 'liability', 'expense', 'revenue'],
    };

    return {
      allGroups: allGroups,
      searchQuery: '',
      activeTab: 'bs',
      currentId: null,
      filterType: 'all',
      excludeId: null,
      modal: null,

      get filteredGroups() {
        var allowed = filterMap[this.filterType] || filterMap.all;
        var exId = this.excludeId;
        return this.allGroups
          .filter(function(g) { return allowed.indexOf(g.type_code) !== -1; })
          .map(function(g) {
            if (!exId) return g;
            return {
              type_code: g.type_code, type_name: g.type_name,
              normal_balance: g.normal_balance,
              accounts: g.accounts.filter(function(a) { return a.id !== exId; }),
            };
          })
          .filter(function(g) { return g.accounts.length > 0; });
      },

      get bsGroups() {
        return this.filteredGroups.filter(function(g) { return bsCodes.indexOf(g.type_code) !== -1; });
      },
      get plGroups() {
        return this.filteredGroups.filter(function(g) { return plCodes.indexOf(g.type_code) !== -1; });
      },
      get hasBs() { return this.bsGroups.length > 0; },
      get hasPl() { return this.plGroups.length > 0; },

      groupsForSide: function(groups, side) {
        return groups.filter(function(g) { return g.normal_balance === side; });
      },

      matchesSearch: function(account) {
        if (!this.searchQuery) return true;
        var q = this.searchQuery.toLowerCase();
        return account.name.toLowerCase().indexOf(q) !== -1 ||
               account.code.toLowerCase().indexOf(q) !== -1;
      },

      open: function(options) {
        options = options || {};
        this.filterType = options.filter || 'all';
        this.excludeId = options.excludeId || null;
        this.currentId = options.currentId ? parseInt(options.currentId) : null;
        this.searchQuery = '';

        var autoTab = null;
        if (this.currentId) {
          for (var i = 0; i < this.filteredGroups.length; i++) {
            var g = this.filteredGroups[i];
            for (var j = 0; j < g.accounts.length; j++) {
              if (g.accounts[j].id === this.currentId) {
                autoTab = bsCodes.indexOf(g.type_code) !== -1 ? 'bs' : 'pl';
                break;
              }
            }
            if (autoTab) break;
          }
        }
        this.activeTab = autoTab || ((options.activeTab === 'pl' && this.hasPl) ? 'pl' : 'bs');

        if (!this.modal) this.modal = new bootstrap.Modal(this.$el);
        this.modal.show();

        if (this.currentId) {
          var el = this.$el;
          el.addEventListener('shown.bs.modal', function _scroll() {
            el.removeEventListener('shown.bs.modal', _scroll);
            var cur = el.querySelector('.acct-current');
            if (cur) cur.scrollIntoView({ block: 'center', behavior: 'instant' });
          });
        }
      },

      select: function(id, name, typeCode) {
        if (window._acctSelectorCallback) {
          window._acctSelectorCallback(id, name, typeCode);
        }
        this.modal.hide();
      }
    };
  });

  /**
   * 取込確認画面: CSV / OFX / Web 共通
   *
   * 使い方:
   *   <div x-data="importConfirm({ rows: [...], paymentAccountId: 1,
   *     defaultIncomeId: 0, defaultExpenseId: 0,
   *     closedPeriods: {}, restrictedBeforeYear: 0 })"
   *     @drag-select-update="_syncFromCheckboxes()">
   */
  Alpine.data('importConfirm', function(config) {
    var closedPeriods = config.closedPeriods || {};
    var restrictedBefore = config.restrictedBeforeYear;
    var paymentAccountId = config.paymentAccountId;
    var defaultIncomeId = config.defaultIncomeId || 0;
    var defaultExpenseId = config.defaultExpenseId || 0;

    function getStatus(row) {
      if (!row.date) return { cls: 'bg-warning text-dark', text: '日付なし', icon: '', problem: false };
      if (!row.deposit && !row.withdrawal) return { cls: 'bg-secondary', text: '金額なし', icon: '', problem: false };
      var year = parseInt(row.date.substring(0, 4));
      var month = parseInt(row.date.substring(5, 7));
      if (restrictedBefore && year < restrictedBefore && closedPeriods[year] === undefined) {
        return { cls: 'bg-warning text-dark', text: '年度未開設', icon: 'exclamation-triangle', problem: true };
      }
      if (closedPeriods[year] !== undefined && month <= closedPeriods[year]) {
        return { cls: 'bg-danger', text: '確定済み', icon: 'lock-fill', problem: true };
      }
      return { cls: 'bg-success', text: 'OK', icon: '', problem: false };
    }

    function applyStatus(row) {
      var st = getStatus(row);
      row.statusCls = st.cls;
      row.statusText = st.text;
      row.statusIcon = st.icon;
      row.hasProblem = st.problem;
      return st;
    }

    return {
      rows: [],
      allChecked: true,
      bulkDate: '',
      appendOriginalDate: true,
      sortKey: null,
      sortDir: 0,
      aiLoading: false,
      hasRestricted: false,

      get selectedCount() {
        var c = 0;
        for (var i = 0; i < this.rows.length; i++) if (this.rows[i].enabled) c++;
        return c;
      },
      get totalDeposit() {
        var s = 0;
        for (var i = 0; i < this.rows.length; i++) if (this.rows[i].enabled) s += this.rows[i].deposit;
        return s;
      },
      get totalWithdrawal() {
        var s = 0;
        for (var i = 0; i < this.rows.length; i++) if (this.rows[i].enabled) s += this.rows[i].withdrawal;
        return s;
      },

      init: function() {
        for (var i = 0; i < config.rows.length; i++) {
          var r = config.rows[i];
          var defCatId = r.deposit ? defaultIncomeId : (r.withdrawal ? defaultExpenseId : 0);
          var st = getStatus(r);
          this.rows.push({
            _origIndex: i,
            row_num: r.row_num || i + 1,
            date: r.date || '',
            description: r.description || '',
            deposit: r.deposit || 0,
            withdrawal: r.withdrawal || 0,
            category_id: defCatId || '',
            category_name: '',
            enabled: !!r.date && (!!r.deposit || !!r.withdrawal) && !st.problem,
            statusCls: st.cls,
            statusText: st.text,
            statusIcon: st.icon,
            hasProblem: st.problem,
            dateEditing: false,
            dateEditValue: '',
          });
        }
        // Resolve default category names
        for (var i = 0; i < this.rows.length; i++) {
          if (this.rows[i].category_id && typeof _acctNameById === 'function') {
            this.rows[i].category_name = _acctNameById(parseInt(this.rows[i].category_id)) || '';
          }
        }
        // Check for restricted years
        for (var i = 0; i < this.rows.length; i++) {
          if (this.rows[i].hasProblem && this.rows[i].statusText === '年度未開設') {
            this.hasRestricted = true; break;
          }
        }
        // Auto-suggest categories
        this._suggestCategories();
        // Initialize drag select after render
        var el = this.$el;
        this.$nextTick(function() {
          if (typeof initDragSelect === 'function') {
            initDragSelect('#confirmTable', '.row-check', function() {
              el.dispatchEvent(new CustomEvent('drag-select-update'));
            });
          }
        });
      },

      toggleAll: function(checked) {
        for (var i = 0; i < this.rows.length; i++) this.rows[i].enabled = checked;
        this.allChecked = checked;
      },

      onRowCheck: function() {
        this.allChecked = this.selectedCount === this.rows.length && this.rows.length > 0;
      },

      sortBy: function(key) {
        if (this.sortKey === key) {
          this.sortDir = (this.sortDir + 1) % 3;
        } else {
          this.sortKey = key;
          this.sortDir = 1;
        }
        if (this.sortDir === 0) {
          this.rows.sort(function(a, b) { return a._origIndex - b._origIndex; });
          this.sortKey = null;
        } else {
          var dir = this.sortDir;
          var isNum = (key === 'row_num' || key === 'deposit' || key === 'withdrawal');
          this.rows.sort(function(a, b) {
            var va = a[key], vb = b[key];
            if (!isNum) { va = String(va).toLowerCase(); vb = String(vb).toLowerCase(); }
            var cmp = isNum ? (va - vb) : (va < vb ? -1 : (va > vb ? 1 : 0));
            return dir === 2 ? -cmp : cmp;
          });
        }
      },

      sortIcon: function(key) {
        if (this.sortKey !== key || this.sortDir === 0) return '';
        return this.sortDir === 1 ? ' \u25B2' : ' \u25BC';
      },

      selectCategory: function(index) {
        var row = this.rows[index];
        openAccountSelector(function(id, name) {
          row.category_id = id;
          row.category_name = name;
        }, { filter: 'category_transfer', excludeId: paymentAccountId, activeTab: 'pl', currentId: row.category_id });
      },

      bulkSetCategory: function() {
        var selected = [];
        for (var i = 0; i < this.rows.length; i++) if (this.rows[i].enabled) selected.push(this.rows[i]);
        if (selected.length === 0) { alert('行を選択してください。'); return; }
        var self = this;
        openAccountSelector(function(id, name) {
          for (var i = 0; i < selected.length; i++) {
            selected[i].category_id = id;
            selected[i].category_name = name;
          }
          self.toggleAll(false);
        }, { filter: 'category_transfer', excludeId: paymentAccountId, activeTab: 'pl' });
      },

      startDateEdit: function(index) {
        var row = this.rows[index];
        if (row.dateEditing) return;
        row.dateEditValue = row.date;
        row.dateEditing = true;
        var origIdx = row._origIndex;
        this.$nextTick(function() {
          var tr = document.querySelector('#confirmTable tr[data-idx="' + origIdx + '"]');
          var input = tr && tr.querySelector('input[type="date"]');
          if (input) input.focus();
        });
      },

      commitDateEdit: function(index) {
        var row = this.rows[index];
        if (!row.dateEditing) return;
        row.dateEditing = false;
        if (row.dateEditValue && row.dateEditValue !== row.date) {
          row.date = row.dateEditValue;
          var st = applyStatus(row);
          if (!st.problem && (row.deposit || row.withdrawal)) row.enabled = true;
        }
      },

      cancelDateEdit: function(index) {
        this.rows[index].dateEditing = false;
      },

      bulkSetDate: function(selectedOnly) {
        if (!this.bulkDate) { alert('日付を入力してください。'); return; }
        if (selectedOnly) {
          var has = false;
          for (var i = 0; i < this.rows.length; i++) { if (this.rows[i].enabled) { has = true; break; } }
          if (!has) { alert('行を選択してください。'); return; }
        }
        for (var i = 0; i < this.rows.length; i++) {
          var row = this.rows[i];
          if (selectedOnly && !row.enabled) continue;
          if (this.appendOriginalDate && row.date) {
            var orig = row.date.replace(/-/g, '/');
            if (row.description.indexOf('(' + row.date + ')') === -1 &&
                row.description.indexOf('（取引日:') === -1) {
              row.description = row.description + '（取引日: ' + orig + '）';
            }
          }
          row.date = this.bulkDate;
          var st = applyStatus(row);
          if (!st.problem && (row.deposit || row.withdrawal)) row.enabled = true;
        }
      },

      _suggestCategories: function() {
        var descriptions = [];
        for (var i = 0; i < this.rows.length; i++) {
          if (this.rows[i].description) descriptions.push(this.rows[i].description);
        }
        if (descriptions.length === 0) return;
        var self = this;
        fetch('/journal/api/suggest-categories', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': Alpine.store('csrf').token,
          },
          body: JSON.stringify({ descriptions: descriptions, payment_account_id: paymentAccountId }),
        })
          .then(function(res) { return res.json(); })
          .then(function(suggestions) {
            if (!suggestions || typeof suggestions !== 'object') return;
            for (var i = 0; i < self.rows.length; i++) {
              var desc = self.rows[i].description;
              if (desc && suggestions[desc]) {
                self.rows[i].category_id = suggestions[desc].account_id;
                self.rows[i].category_name = suggestions[desc].account_name;
              }
            }
          })
          .catch(function() { /* ignore */ });
      },

      aiSuggestCategories: function() {
        var targets = [];
        var indices = [];
        for (var i = 0; i < this.rows.length; i++) {
          var row = this.rows[i];
          if (row.category_id && !row.enabled) continue;
          if (!row.description) continue;
          targets.push({ description: row.description, deposit: row.deposit, withdrawal: row.withdrawal });
          indices.push(i);
        }
        if (targets.length === 0) {
          alert('対象の行がありません。科目未設定の行、またはチェックした行が必要です。');
          return;
        }
        this.aiLoading = true;
        var self = this;
        fetch('/journal/api/ai-suggest-categories', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': Alpine.store('csrf').token,
          },
          body: JSON.stringify({ payment_account_id: paymentAccountId, rows: targets }),
        })
          .then(function(res) { return res.json(); })
          .then(function(data) {
            if (data.error) { alert(data.error); return; }
            for (var i = 0; i < indices.length; i++) {
              var row = self.rows[indices[i]];
              var sugg = data[row.description];
              if (sugg) { row.category_id = sugg.account_id; row.category_name = sugg.account_name; }
            }
          })
          .catch(function(err) { alert('AI科目推定に失敗しました: ' + err.message); })
          .finally(function() { self.aiLoading = false; });
      },

      serializeRows: function() {
        var sorted = this.rows.slice().sort(function(a, b) { return a._origIndex - b._origIndex; });
        var result = [];
        for (var i = 0; i < sorted.length; i++) {
          var row = sorted[i];
          result.push({
            enabled: row.enabled,
            date: row.date,
            description: row.description,
            deposit: row.deposit,
            withdrawal: row.withdrawal,
            category_id: row.category_id ? parseInt(row.category_id) : 0,
          });
        }
        this.$refs.importRows.value = JSON.stringify(result);
      },

      _syncFromCheckboxes: function() {
        var cbs = this.$el.querySelectorAll('.row-check');
        for (var j = 0; j < cbs.length; j++) {
          var origIdx = parseInt(cbs[j].dataset.idx);
          for (var i = 0; i < this.rows.length; i++) {
            if (this.rows[i]._origIndex === origIdx) {
              this.rows[i].enabled = cbs[j].checked;
              break;
            }
          }
        }
        this.allChecked = this.selectedCount === this.rows.length && this.rows.length > 0;
      }
    };
  });

});
