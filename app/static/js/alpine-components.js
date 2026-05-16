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
      editingCode: null,
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
      transferToCode: '',
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
        this.editingCode = null;
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
        this.transferToCode = '';
      },

      open: function(accountCode, copy) {
        this.resetForm();
        if (accountCode) {
          var self = this;
          var url = '/accounts/api/' + accountCode;
          if (copy) url += '?copy=1';
          fetch(url)
            .then(function(r) { return r.json(); })
            .then(function(data) {
              self.editingCode = copy ? null : data.code;
              self.editingTypeId = data.account_type_id;
              self.wasActive = copy ? true : data.is_active;
              self.modalTitle = copy ? 'コピーして追加' : '科目を編集';
              self.code = data.code;
              self.codeReadOnly = false;
              self.name = data.name;
              self.accountTypeId = String(data.account_type_id);
              self.typeDisabled = false;
              self.description = data.description;
              self.taxCategory = data.tax_category;
              self.costType = data.cost_type;
              self.isActive = true;
              self.activeDisabled = false;
              if (!copy) {
                self.codeReadOnly = data.is_system;
                self.typeDisabled = data.is_system;
                self.isActive = data.is_active;
                self.activeDisabled = !!data.system_role;
              }
              self.getModal().show();
            });
        } else {
          this.modalTitle = '科目を追加';
          this.getModal().show();
        }
      },

      onActiveChange: function() {
        if (!this.isActive && this.editingCode && this.wasActive) {
          this.showDeactivate = true;
          this.deactivateLoading = true;
          this.hasBalance = false;
          var self = this;
          fetch('/accounts/api/' + this.editingCode + '/balance')
            .then(function(r) { return r.json(); })
            .then(function(data) {
              self.deactivateLoading = false;
              if (data.balance !== 0) {
                self.hasBalance = true;
                self.balanceLabel = '\u00a5' + Math.abs(data.balance).toLocaleString() +
                  (data.balance < 0 ? '（貸方残）' : '（借方残）');
                var candidates = self.accountsByType[self.editingTypeId] || [];
                self.transferCandidates = candidates.filter(function(a) {
                  return a.code !== self.editingCode;
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
        if (!this.isActive && this.editingCode && this.wasActive && this.transferToCode) {
          payload.transfer_to_account_code = this.transferToCode;
        }
        var url = this.editingCode ? ('/accounts/api/' + this.editingCode) : '/accounts/api/new';
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
        if (data.account_code) {
          name = typeof _acctNameByCode === 'function'
            ? _acctNameByCode(data.account_code, config.fullName) : '';
          if (!name && data.account_name) name = data.account_name;
          if (data.is_proprietor && !name) name = '事業主';
        }
        this.lines.push({
          _key: ++_keyCounter,
          account_code: data.account_code || '',
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
        openAccountSelector(function(code, name) {
          line.account_code = code;
          line.account_name = name;
        }, {filter: filter || 'all', currentCode: line.account_code});
      },

      serializeLines: function() {
        var result = [];
        for (var i = 0; i < this.lines.length; i++) {
          var line = this.lines[i];
          var debit = parseInt(line.debit_amount) || 0;
          var credit = parseInt(line.credit_amount) || 0;
          if (line.account_code && (debit > 0 || credit > 0)) {
            result.push({
              account_code: line.account_code,
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
      expense: ['expense'],
      revenue: ['revenue'],
      category_transfer: ['asset', 'liability', 'expense', 'revenue'],
    };

    return {
      allGroups: allGroups,
      searchQuery: '',
      activeTab: 'bs',
      currentCode: null,
      filterType: 'all',
      excludeCode: null,
      modal: null,

      get filteredGroups() {
        var allowed = filterMap[this.filterType] || filterMap.all;
        var exCode = this.excludeCode;
        return this.allGroups
          .filter(function(g) { return allowed.indexOf(g.type_code) !== -1; })
          .map(function(g) {
            if (!exCode) return g;
            return {
              type_code: g.type_code, type_name: g.type_name,
              normal_balance: g.normal_balance,
              accounts: g.accounts.filter(function(a) { return a.code !== exCode; }),
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
        this.excludeCode = options.excludeCode || null;
        this.currentCode = options.currentCode || null;
        this.searchQuery = '';

        var autoTab = null;
        if (this.currentCode) {
          for (var i = 0; i < this.filteredGroups.length; i++) {
            var g = this.filteredGroups[i];
            for (var j = 0; j < g.accounts.length; j++) {
              if (g.accounts[j].code === this.currentCode) {
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

        if (this.currentCode) {
          var el = this.$el;
          el.addEventListener('shown.bs.modal', function _scroll() {
            el.removeEventListener('shown.bs.modal', _scroll);
            var cur = el.querySelector('.acct-current');
            if (cur) cur.scrollIntoView({ block: 'center', behavior: 'instant' });
          });
        }
      },

      select: function(code, name, typeCode) {
        if (window._acctSelectorCallback) {
          var displayName = (typeof _acctNameByCode === 'function' ? _acctNameByCode(code) : '') || name;
          window._acctSelectorCallback(code, displayName, typeCode);
        }
        this.modal.hide();
      }
    };
  });

  /**
   * CSV照合モード: 取込/照合タブの切り替え + 照合結果管理
   *
   * 使い方:
   *   <div x-data="reconcileMode({
   *     csvRows: [...], paymentAccountCode: '1010',
   *     defaultIncomeId: 0, defaultExpenseId: 0
   *   })">
   */
  Alpine.data('reconcileMode', function(config) {
    var csvRows = config.csvRows || [];
    var paymentAccountCode = config.paymentAccountCode;
    var defaultIncomeId = config.defaultIncomeId || 0;
    var defaultExpenseId = config.defaultExpenseId || 0;
    var hasAiConfig = config.hasAiConfig || false;
    var sourceLabels = {
      'journal': '仕訳', 'cashbook': '出納帳', 'ai_receipt': 'AI証憑',
      'csv': 'CSV', 'ofx': 'OFX', 'web': 'Web', 'closing': '決算',
    };

    return {
      activeTab: 'import',
      reconcileLoaded: false,
      reconcileLoading: false,
      reconcileRows: [],
      dailySummary: [],
      journalOnly: [],
      hiddenJournalOnlyIds: [],
      hasAiConfig: hasAiConfig,
      aiReconcileLoading: false,
      hoveredDay: null,
      _hoverTimer: null,

      hoverEnter: function(dateStr) {
        var self = this;
        clearTimeout(this._hoverTimer);
        this.hoveredDay = dateStr;
      },
      hoverLeave: function() {
        var self = this;
        this._hoverTimer = setTimeout(function() { self.hoveredDay = null; }, 150);
      },

      getDayDiff: function(dateStr) {
        if (!dateStr) return { csv: [], journal: [] };
        var csv = [];
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var r = this.reconcileRows[i];
          if (r.date === dateStr) {
            csv.push({
              description: r.description,
              amount: r.withdrawal || r.deposit || 0,
              status: r.status,
              matchInfo: r.matchInfo,
            });
          }
        }
        csv.sort(function(a, b) { return b.amount - a.amount; });
        var journal = [];
        // journal_only
        for (var i = 0; i < this.journalOnly.length; i++) {
          var j = this.journalOnly[i];
          if (j.date === dateStr) {
            journal.push({
              description: j.description, amount: j.amount,
              category_name: j.category_name, source: j.source, matched: false,
            });
          }
        }
        // matched 仕訳
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var r = this.reconcileRows[i];
          if (r.date === dateStr && r.status === 'matched' && r.matchInfo) {
            journal.push({
              description: r.matchInfo.description, amount: r.matchInfo.amount,
              category_name: r.matchInfo.category_name, source: r.matchInfo.source,
              matched: true,
            });
          }
        }
        journal.sort(function(a, b) { return b.amount - a.amount; });
        return { csv: csv, journal: journal };
      },

      get matchedCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++)
          if (this.reconcileRows[i].status === 'matched') c++;
        return c;
      },
      get matchedExactCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var r = this.reconcileRows[i];
          if (r.status === 'matched' && r.matchInfo &&
              (!r.matchInfo.date_band || r.matchInfo.date_band === 'exact')) c++;
        }
        return c;
      },
      get matchedWarnCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var r = this.reconcileRows[i];
          if (r.status === 'matched' && r.matchInfo &&
              r.matchInfo.date_band === 'warn') c++;
        }
        return c;
      },
      get matchedCautionCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var r = this.reconcileRows[i];
          if (r.status === 'matched' && r.matchInfo &&
              r.matchInfo.date_band === 'caution') c++;
        }
        return c;
      },
      get multipleCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++)
          if (this.reconcileRows[i].status === 'multiple') c++;
        return c;
      },
      get unmatchedCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++)
          if (this.reconcileRows[i].status === 'unmatched') c++;
        return c;
      },
      get summaryTotals() {
        var t = { csv_count: 0, csv_total: 0, journal_count: 0, journal_total: 0, diff_count: 0, diff_amount: 0 };
        for (var i = 0; i < this.dailySummary.length; i++) {
          var d = this.dailySummary[i];
          t.csv_count += d.csv_count;
          t.csv_total += d.csv_total;
          t.journal_count += d.journal_count;
          t.journal_total += d.journal_total;
        }
        t.diff_count = t.csv_count - t.journal_count;
        t.diff_amount = t.csv_total - t.journal_total;
        return t;
      },
      get discrepancyCount() {
        var c = 0;
        for (var i = 0; i < this.dailySummary.length; i++)
          if (this.dailySummary[i].has_discrepancy) c++;
        return c;
      },
      get journalOnlyCount() {
        return this.journalOnly.length;
      },
      get visibleJournalOnly() {
        var hidden = this.hiddenJournalOnlyIds;
        if (!hidden.length) return this.journalOnly;
        return this.journalOnly.filter(function(j) {
          return hidden.indexOf(j.entry_id) === -1;
        });
      },
      get journalOnlyTotal() {
        var total = 0;
        var visible = this.visibleJournalOnly;
        for (var i = 0; i < visible.length; i++) total += visible[i].amount || 0;
        return total;
      },
      get reconcileImportCount() {
        var c = 0;
        for (var i = 0; i < this.reconcileRows.length; i++)
          if (this.reconcileRows[i].enabled) c++;
        return c;
      },

      sourceLabel: function(src) {
        return sourceLabels[src] || src;
      },

      hideJournalOnly: function(entryId) {
        if (this.hiddenJournalOnlyIds.indexOf(entryId) === -1) {
          this.hiddenJournalOnlyIds.push(entryId);
        }
      },
      resetHiddenJournalOnly: function() {
        this.hiddenJournalOnlyIds = [];
      },

      switchTab: function(tab) {
        this.activeTab = tab;
        if (tab === 'reconcile' && !this.reconcileLoaded) {
          this.loadReconciliation();
        }
      },

      loadReconciliation: function() {
        this.reconcileLoading = true;
        var self = this;
        fetch('/csv-import/reconcile', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': Alpine.store('csrf').token,
          },
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
          if (data.error) { alert(data.error); return; }
          self.dailySummary = data.daily_summary || [];
          self.journalOnly = data.journal_only || [];
          var csvResults = data.csv_results || [];
          self.reconcileRows = [];
          for (var i = 0; i < csvResults.length; i++) {
            var r = csvResults[i];
            var csv = csvRows[r.csv_index] || {};
            var row = {
              csv_index: r.csv_index,
              date: csv.date || '',
              description: csv.description || '',
              deposit: csv.deposit || 0,
              withdrawal: csv.withdrawal || 0,
              status: r.status,
              matches: r.matches,
              selectedMatchIndex: -1,
              enabled: false,
              matchInfo: null,
              category_code: '',
              category_name: '',
              snapping: false,
            };
            if (r.status === 'matched') {
              row.selectedMatchIndex = 0;
              row.matchInfo = r.matches[0];
            } else if (r.status === 'multiple') {
              row.selectedMatchIndex = 0;
              row.matchInfo = r.matches[0];
            } else {
              row.enabled = !!(csv.date && (csv.deposit || csv.withdrawal));
              row.category_code = csv.deposit ? defaultIncomeId : (csv.withdrawal ? defaultExpenseId : 0);
            }
            self.reconcileRows.push(row);
          }
          for (var i = 0; i < self.reconcileRows.length; i++) {
            if (self.reconcileRows[i].category_code && typeof _acctNameByCode === 'function') {
              self.reconcileRows[i].category_name = _acctNameByCode(String(self.reconcileRows[i].category_code)) || '';
            }
          }
          self.reconcileLoaded = true;
        })
        .catch(function(err) { alert('照合に失敗しました: ' + err.message); })
        .finally(function() { self.reconcileLoading = false; });
      },

      snapDate: function(rowIdx) {
        var row = this.reconcileRows[rowIdx];
        if (!row || !row.matchInfo || !row.date) return;
        if (row.snapping) return;
        row.snapping = true;
        var self = this;
        fetch('/csv-import/match/snap-date', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': Alpine.store('csrf').token,
          },
          body: JSON.stringify({
            entry_id: row.matchInfo.entry_id,
            csv_date: row.date,
          }),
        })
        .then(function(res) {
          return res.json().then(function(body) { return { ok: res.ok, body: body }; });
        })
        .then(function(r) {
          if (!r.ok || !r.body.success) {
            showToast(r.body.error || '日付の更新に失敗しました', 'danger');
            return;
          }
          // matchInfo を在席のまま更新（exact に昇格）
          row.matchInfo.date = r.body.new_date;
          row.matchInfo.date_diff_days = 0;
          row.matchInfo.date_band = 'exact';
          showToast('仕訳の日付を ' + r.body.new_date + ' に変更しました', 'success');
        })
        .catch(function(err) {
          showToast('日付の更新に失敗しました: ' + err.message, 'danger');
        })
        .finally(function() { row.snapping = false; });
      },

      selectMatch: function(rowIdx, matchIdx) {
        var row = this.reconcileRows[rowIdx];
        row.selectedMatchIndex = matchIdx;
        if (matchIdx >= 0) {
          row.enabled = false;
          row.matchInfo = row.matches[matchIdx];
          row.category_code = '';
          row.category_name = '';
        } else {
          row.enabled = true;
          row.matchInfo = null;
          var csv = csvRows[row.csv_index] || {};
          row.category_code = csv.deposit ? defaultIncomeId : (csv.withdrawal ? defaultExpenseId : 0);
          if (row.category_code && typeof _acctNameByCode === 'function') {
            row.category_name = _acctNameByCode(String(row.category_code)) || '';
          }
        }
      },

      selectReconcileCategory: function(index) {
        var row = this.reconcileRows[index];
        openAccountSelector(function(code, name) {
          row.category_code = code;
          row.category_name = name;
        }, { filter: 'category_transfer', excludeCode: paymentAccountCode, activeTab: 'pl', currentCode: row.category_code });
      },

      toggleAllReconcile: function(checked) {
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var row = this.reconcileRows[i];
          // matched や候補選択済みは操作不可
          if (row.status === 'matched') continue;
          if (row.status === 'multiple' && row.selectedMatchIndex >= 0) continue;
          row.enabled = checked;
        }
      },

      runAiReconcile: function() {
        this.aiReconcileLoading = true;
        var self = this;
        fetch('/csv-import/ai-reconcile', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': Alpine.store('csrf').token,
          },
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
          if (data.error) { showToast(data.error, 'danger'); return; }
          var matches = data.matches || [];
          for (var m = 0; m < matches.length; m++) {
            var ai = matches[m];
            for (var r = 0; r < self.reconcileRows.length; r++) {
              var row = self.reconcileRows[r];
              if (row.csv_index === ai.csv_index && row.status === 'unmatched') {
                // journal_only から該当仕訳の情報を取得
                var jnl = null;
                for (var j = 0; j < self.journalOnly.length; j++) {
                  if (self.journalOnly[j].entry_id === ai.entry_id) {
                    jnl = self.journalOnly[j];
                    break;
                  }
                }
                if (jnl) {
                  row.status = 'ai_suggested';
                  row.matches = [{
                    entry_id: jnl.entry_id,
                    date: jnl.date,
                    description: jnl.description,
                    amount: jnl.amount,
                    source: jnl.source,
                    category_name: jnl.category_name,
                    confidence: ai.confidence,
                    reason: ai.reason,
                  }];
                  row.selectedMatchIndex = 0;
                  row.matchInfo = row.matches[0];
                  row.enabled = false;
                }
                break;
              }
            }
          }
          if (matches.length === 0) { showToast('AI照合候補が見つかりませんでした。', 'info'); }
          else { showToast('AI照合: ' + matches.length + '件の候補が見つかりました。', 'success'); }
        })
        .catch(function(err) { showToast('AI照合に失敗しました: ' + err.message, 'danger'); })
        .finally(function() { self.aiReconcileLoading = false; });
      },

      serializeReconcileRows: function() {
        var result = [];
        for (var i = 0; i < this.reconcileRows.length; i++) {
          var row = this.reconcileRows[i];
          result.push({
            enabled: row.enabled,
            date: row.date,
            description: row.description,
            deposit: row.deposit,
            withdrawal: row.withdrawal,
            category_code: row.category_code || '',
          });
        }
        this.$refs.reconcileImportRows.value = JSON.stringify(result);
      },
    };
  });

  /**
   * 取込確認画面: CSV / OFX / Web 共通
   *
   * 使い方:
   *   <div x-data="importConfirm({ rows: [...], paymentAccountCode: '1010',
   *     defaultIncomeId: 0, defaultExpenseId: 0,
   *     closedPeriods: {}, restrictedBeforeYear: 0 })"
   *     @drag-select-update="_syncFromCheckboxes()">
   */
  Alpine.data('importConfirm', function(config) {
    var closedPeriods = config.closedPeriods || {};
    var restrictedBefore = config.restrictedBeforeYear;
    var paymentAccountCode = config.paymentAccountCode;
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
            category_code: defCatId || '',
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
          if (this.rows[i].category_code && typeof _acctNameByCode === 'function') {
            this.rows[i].category_name = _acctNameByCode(String(this.rows[i].category_code)) || '';
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
        openAccountSelector(function(code, name) {
          row.category_code = code;
          row.category_name = name;
        }, { filter: 'category_transfer', excludeCode: paymentAccountCode, activeTab: 'pl', currentCode: row.category_code });
      },

      bulkSetCategory: function() {
        var selected = [];
        for (var i = 0; i < this.rows.length; i++) if (this.rows[i].enabled) selected.push(this.rows[i]);
        if (selected.length === 0) { alert('行を選択してください。'); return; }
        var self = this;
        openAccountSelector(function(code, name) {
          for (var i = 0; i < selected.length; i++) {
            selected[i].category_code = code;
            selected[i].category_name = name;
          }
          self.toggleAll(false);
        }, { filter: 'category_transfer', excludeCode: paymentAccountCode, activeTab: 'pl' });
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
          body: JSON.stringify({ descriptions: descriptions, payment_account_code: paymentAccountCode }),
        })
          .then(function(res) { return res.json(); })
          .then(function(suggestions) {
            if (!suggestions || typeof suggestions !== 'object') return;
            for (var i = 0; i < self.rows.length; i++) {
              var desc = self.rows[i].description;
              if (desc && suggestions[desc]) {
                self.rows[i].category_code = suggestions[desc].account_code;
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
          if (row.category_code && !row.enabled) continue;
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
          body: JSON.stringify({ payment_account_code: paymentAccountCode, rows: targets }),
        })
          .then(function(res) { return res.json(); })
          .then(function(data) {
            if (data.error) { alert(data.error); return; }
            for (var i = 0; i < indices.length; i++) {
              var row = self.rows[indices[i]];
              var sugg = data[row.description];
              if (sugg) { row.category_code = sugg.account_code; row.category_name = sugg.account_name; }
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
            category_code: row.category_code || '',
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
