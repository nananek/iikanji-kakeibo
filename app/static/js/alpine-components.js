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
   * 使い方 (旧 form POST 経路, ai_journal/review.html 等):
   *   <form x-data="journalLines({ lines: [...], fullName: false })"
   *         @submit="serializeLines()">
   *     <input type="hidden" name="lines_json" x-ref="linesJson">
   *     <template x-for="(line, index) in lines" :key="line._key"> ... </template>
   *
   * 使い方 (E3-F PR-B2: journal/form.html, JS submit + 暗号化経路):
   *   <form x-data="journalLines({ lines: [...], submitConfig: {
   *           isEdit, entryId, userId, isProxyMode } })"
   *         @submit.prevent="submitJournalForm($event)">
   *   submitConfig が指定された場合のみ submitJournalForm が動作する。
   */
  Alpine.data('journalLines', function(config) {
    var _keyCounter = 0;

    return {
      lines: [],
      submitting: false,
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
      },

      // E3-F PR-B2: journal/form.html 用の JS submit。
      // config.submitConfig が指定された時のみ動作する。is_proprietor 行は除外
      // (Lv2 監査用の集約行で、サーバ側に送るとそもそも科目が存在しないため)。
      async submitJournalForm(event) {
        var sc = config && config.submitConfig;
        if (!sc) return;
        if (this.submitting) return;
        if (sc.isProxyMode) {
          alert('代理閲覧モードでは保存できません。本人アカウントで実行してください。');
          return;
        }
        var formEl = event && event.target;
        if (!formEl) return;
        var dateEl = formEl.querySelector('input[name=date]');
        var fpEl = formEl.querySelector('select[name=fiscal_period]');
        var descEl = formEl.querySelector('input[name=description], textarea[name=description]');
        var fp = (fpEl && fpEl.value !== '') ? parseInt(fpEl.value, 10) : null;
        var sentLines = [];
        for (var i = 0; i < this.lines.length; i++) {
          var ln = this.lines[i];
          if (ln.is_proprietor) continue;
          sentLines.push({
            account_code: ln.account_code,
            debit_amount: parseInt(ln.debit_amount) || 0,
            credit_amount: parseInt(ln.credit_amount) || 0,
            description: ln.description || '',
          });
        }
        this.submitting = true;
        try {
          await window.journalSubmitE2EE({
            isEdit: sc.isEdit,
            entryId: sc.entryId,
            userId: sc.userId,
            date: dateEl ? dateEl.value : '',
            fiscalPeriod: fp,
            description: descEl ? descEl.value : '',
            lines: sentLines,
          });
        } catch (err) {
          this.submitting = false;
          alert('保存に失敗しました: ' + (err && err.message ? err.message : err));
        }
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
    var userId = config.userId;
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
        // E3-F PR-D-2: 照合は client 側 classical.findMatches で実行する。
        // GET /api/v1/journals を年度別に取得・復号し、平文 date/description/
        // source を読まずにマッチングする。snap 用に復号済み entry も保持。
        runReconcileClassical({
          userId: userId,
          paymentAccountCode: paymentAccountCode,
          csvRows: csvRows,
        })
        .then(function(ret) {
          var data = ret.result;
          self._entriesById = ret.entriesById;
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
        // E3-F PR-D-2: 旧 POST /csv-import/match/snap-date は平文 date のみを
        // 書き換え暗号化 blob を更新しないため client から見て無効だった。
        // 復号済み entry を新日付で再暗号化し PUT /api/v1/journals/<id> する。
        var entry = self._entriesById && self._entriesById[row.matchInfo.entry_id];
        if (!entry) {
          showToast('対象仕訳が見つかりません。再読込してください。', 'danger');
          row.snapping = false;
          return;
        }
        snapJournalDateE2EE({
          userId: userId,
          entryId: entry.id,
          newDate: row.date,
          entry: entry,
        })
        .then(function() {
          // ローカルの復号済み entry も更新し、再照合時の整合を保つ。
          entry.date = row.date;
          // matchInfo を在席のまま更新（exact に昇格）
          row.matchInfo.date = row.date;
          row.matchInfo.date_diff_days = 0;
          row.matchInfo.date_band = 'exact';
          showToast('仕訳の日付を ' + row.date + ' に変更しました', 'success');
        })
        .catch(function(err) {
          showToast('日付の更新に失敗しました: ' + (err.message || err), 'danger');
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
        // 未照合 CSV 行と仕訳候補は client 側 classical の結果から渡す
        // (E3-F PR-D-2: サーバは平文を読まない)。
        var unmatched = [];
        for (var r = 0; r < self.reconcileRows.length; r++) {
          var row = self.reconcileRows[r];
          if (row.status !== 'unmatched') continue;
          var amount = row.withdrawal || row.deposit || 0;
          if (!amount) continue;
          unmatched.push({
            csv_index: row.csv_index,
            date: row.date || '',
            description: row.description || '',
            amount: amount,
          });
        }
        // クライアント完結 E2EE フローで実行。サーバには
        // raw description / API キーが届かない。
        runReconcileE2EE({ unmatched: unmatched, candidates: self.journalOnly })
        .then(function(matches) {
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
        .catch(function(err) { showToast('AI照合に失敗しました: ' + (err.message || err), 'danger'); })
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
    // E3-D-3b: 取込種別 (cashbook 仕訳の source 列に入る値)。
    // 'web' / 'csv' / 'ofx' のいずれかを呼出側 (confirm.html) から渡す。
    var importSource = config.importSource || 'web';
    // old_year_action=capital を扱うときに必要な元入金 (capital) 科目コード。
    // 未開設年度の行を当年 1/1 / 元入金科目に差替えてから batch API に送る。
    var capitalCode = config.capitalCode || '';
    // E3-F PR-A: クライアント側暗号化の AAD に使う user_id。テンプレート側で
    // `current_user.id` を渡す。代理閲覧 (auditor proxy) では監査者本人の ID
    // が渡るが、書込み先 DB は owner なので AAD 不一致で復号不能になる。
    // isProxyMode が true の時は submitImportBatch 冒頭で fail-loud に拒否する。
    var userId = config.userId;
    var isProxyMode = !!config.isProxyMode;

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
        // クライアント完結 E2EE フローで実行。サーバには
        // description / API キーが届かない。
        runSuggestCategoriesE2EE(paymentAccountCode, targets)
          .then(function(data) {
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

      /**
       * E3-D-3b: 取込確認画面の汎用バッチ確定処理 (web/csv/ofx 共通)。
       *
       * server (xxx_import.confirm POST) で仕訳化していた処理を
       * クライアント entries_builder + batch API に切替える。
       *
       * 振替判定はサーバの create_transfer_entry と等価だが、生成される仕訳は
       * cashbook (income/expense) と借方/貸方が同一になるため、buildCashbookEntry
       * のみを使う (transactionType を deposit/withdrawal で決定)。
       *
       * 確定済み期間 / 未開設年度 / 提出済み科目 のチェックは batch API 側で
       * 実行されエラー時は 400 が返るので、ここではユーザーに表示するだけ。
       *
       * 監査代理閲覧時は呼出側 confirm.html で submit_handler 上書きをスキップ
       * して旧サーバ POST 経路にフォールバックする (本メソッドは呼ばれない)。
       */
      submitImportBatch: async function(event) {
        event.preventDefault();
        // E3-F PR-A: 代理閲覧 (auditor proxy) 中は AAD に監査者の userId が
        // 入って owner DB に保存されると復号不能になる。早期に拒否する。
        if (isProxyMode) {
          alert('代理閲覧モードでは取込できません。本人アカウントで実行してください。');
          return;
        }
        // 未開設年度の扱い: ラジオボタン (skip / capital) の値を読む
        var oldYearActionEl = this.$el.querySelector(
          'input[name="old_year_action"]:checked',
        );
        var oldYearAction = oldYearActionEl ? oldYearActionEl.value : 'skip';
        var todayYear = new Date().getFullYear();
        // 取込対象行をフィルタ + 未開設年度の処理 (skip or capital 変換)
        var validRows = [];
        for (var i = 0; i < this.rows.length; i++) {
          var r = this.rows[i];
          if (!r.enabled || !r.date) continue;
          var amt = (r.deposit && r.deposit > 0) ? r.deposit : r.withdrawal;
          if (!amt || amt <= 0 || !r.category_code) continue;

          var year = parseInt(r.date.substring(0, 4), 10);
          var isClosedYear = restrictedBefore && year < restrictedBefore
              && closedPeriods[year] === undefined;
          if (isClosedYear) {
            if (oldYearAction === 'capital' && capitalCode) {
              // 当年 1/1 / 元入金科目に変換 (server 旧フローと同等)
              validRows.push({
                date: todayYear + '-01-01',
                description: '(' + r.date + ') ' + (r.description || ''),
                deposit: r.deposit, withdrawal: r.withdrawal,
                category_code: capitalCode,
              });
            }
            // 'skip' or capitalCode 未定義 → 除外
            continue;
          }
          validRows.push(r);
        }
        if (validRows.length === 0) {
          alert('取込可能な行がありません (日付・金額・費目が揃った行が対象)。');
          return;
        }
        var submitBtn = this.$el.querySelector('button[type="submit"]');
        var originalLabel = submitBtn ? submitBtn.innerHTML : '';
        if (submitBtn) {
          submitBtn.disabled = true;
          submitBtn.innerHTML =
            '<span class="spinner-border spinner-border-sm" role="status"></span> 取込中...';
        }
        var sharedClient = null;
        try {
          var builderMod = await import("/static/js/crypto/entries_builder.js");
          // E3-F PR-A: entry + 各 line を MK で暗号化して batch API に送る。
          // 旧経路 (平文 POST) は dual-storage 期間中サーバ側で受け付けるが、
          // クライアントは必ず暗号化する (= 平文 POST 経路に意図せず戻らない)。
          if (typeof userId !== 'number' || !Number.isSafeInteger(userId)) {
            throw new Error(
              '取込確認画面の userId が未設定です (テンプレート修正が必要)',
            );
          }
          var sharedClientMod = await import("/static/js/crypto/shared-client.js");
          sharedClient = new sharedClientMod.SharedCryptoClient(
            "/static/js/crypto/shared-worker.js",
          );
          var keyStatus = await sharedClient.status();
          if (!keyStatus.hasKey) {
            throw new Error('MK ロック中です (設定 → 暗号鍵管理 で解除)');
          }
          var entries = [];
          for (var i = 0; i < validRows.length; i++) {
            var r = validRows[i];
            var amount = (r.deposit && r.deposit > 0) ? r.deposit : r.withdrawal;
            entries.push(await builderMod.buildCashbookEntry({
              client: sharedClient,
              userId: userId,
              date: r.date,
              // batch API は空 description を 400 で弾くため、AI 抽出結果が
              // 空のときはフォールバック文字列で補う (旧サーバ confirm POST
              // は空文字を許容していたデグレ回避)
              description: r.description || '(摘要なし)',
              transactionType: (r.deposit && r.deposit > 0) ? 'income' : 'expense',
              paymentAccountCode: paymentAccountCode,
              categoryAccountCode: r.category_code,
              amount: amount,
              source: importSource,
            }));
          }

          var csrfMeta = document.querySelector('meta[name="csrf-token"]');
          var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
          var res = await fetch('/api/v1/journals/batch', {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/json',
              'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ entries: entries }),
          });
          var body = await res.json().catch(function() { return {}; });
          if (!res.ok) {
            throw new Error(body.error || ('HTTP ' + res.status));
          }
          // 将来 upload→sessionStorage 化したときに parsed を掃除する想定の
          // hook (現状は server session でのみ保持されるので空 op)
          try {
            sessionStorage.removeItem(importSource + 'Import:parsed');
          } catch (_e) { /* ignore */ }
          // ナビゲーション後の cashbook ページで表示する成功フラッシュ。
          // server flash の代替 (旧 confirm POST が flash していたものを
          // クライアント完結フローでも維持)。base.html の DOMContentLoaded
          // hook が読んで showToast を呼ぶ。
          var importedCount = (body && body.created_count) || validRows.length;
          try {
            sessionStorage.setItem(
              'flash:success',
              importedCount + '件を取り込みました。',
            );
          } catch (_e) { /* ignore */ }
          window.location.href = '/cashbook/';
        } catch (err) {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = originalLabel;
          }
          alert('取込に失敗しました: ' + (err.message || err));
        } finally {
          if (sharedClient) {
            try { sharedClient.close(); } catch (_e) { /* ignore */ }
          }
        }
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

  /**
   * AI 証憑下書き「案 1 で登録」ボタン (ai_journal/drafts.html) の Alpine
   * component (E3-F PR-B3)。クライアント側で AES-GCM 暗号化して batch API
   * に投げる。成功時は親 .col-md-6 を fade out + DOM 除去。
   *
   * config:
   *   draftId, userId, isProxyMode, suggestion
   *   suggestion: { date, entry_description, lines: [{ account_code,
   *                  debit_amount, credit_amount, description }, ...] }
   */
  Alpine.data('aiDraftQuickAccept', function(config) {
    return {
      submitting: false,

      async submit() {
        if (this.submitting) return;
        if (config.isProxyMode) {
          alert('代理閲覧モードでは登録できません。本人アカウントで実行してください。');
          return;
        }
        var s = config.suggestion;
        if (!s) {
          if (typeof showToast === 'function') {
            showToast('解析データがありません。', 'danger');
          }
          return;
        }
        var description = (s.entry_description || '').trim();
        var date = s.date || '';
        var rawLines = s.lines || [];
        var lines = [];
        for (var i = 0; i < rawLines.length; i++) {
          var ln = rawLines[i];
          var code = ln.account_code;
          var debit = parseInt(ln.debit_amount, 10) || 0;
          var credit = parseInt(ln.credit_amount, 10) || 0;
          if (!code) continue;
          if (debit === 0 && credit === 0) continue;
          lines.push({
            account_code: code,
            debit: debit,
            credit: credit,
            description: ln.description || '',
          });
        }
        if (!date || !description || lines.length < 2) {
          if (typeof showToast === 'function') {
            showToast(
              '案 1 の内容が不完全です。レビュー画面で確認してください。',
              'danger',
            );
          }
          return;
        }
        this.submitting = true;
        var self = this;
        try {
          var entryNumber = await window.aiDraftQuickAcceptE2EE({
            draftId: config.draftId,
            userId: config.userId,
            date: date,
            description: description,
            lines: lines,
          });
          if (typeof showToast === 'function') {
            showToast(
              '伝票 #' + entryNumber + ' を登録しました。',
              'success',
            );
          }
          var card = self.$el.closest('.col-md-6');
          if (card) {
            card.style.transition = 'opacity 0.3s';
            card.style.opacity = '0';
            setTimeout(function() { card.remove(); }, 300);
          }
        } catch (err) {
          self.submitting = false;
          var msg = (err && err.message) ? err.message : '登録に失敗しました。';
          if (typeof showToast === 'function') {
            showToast(msg, 'danger');
          } else {
            alert(msg);
          }
        }
      }
    };
  });

});


// importConfirm.aiSuggestCategories から呼ばれる E2EE
// クライアント完結フロー。サーバには description/API キー一切送らない。
async function runSuggestCategoriesE2EE(paymentAccountCode, rows) {
  var orchestratorMod = await import("/static/js/crypto/suggest_categories_orchestrator.js");
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var workerUrl = "/static/js/crypto/shared-worker.js";
  var client = new sharedClientMod.SharedCryptoClient(workerUrl);
  try {
    var status = await client.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    return await orchestratorMod.runSuggestCategories({
      paymentAccountCode: paymentAccountCode,
      rows: rows,
      client: client,
    });
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
window.runSuggestCategoriesE2EE = runSuggestCategoriesE2EE;


// cashbook/form.html (新規 + 編集) から呼ばれる E2EE submit。
//
// 出納帳 form を JS submit に乗っ取り、入力値を entries_builder で暗号化して
// batch API (新規: POST /api/v1/journals/batch) / PUT API (編集:
// PUT /api/v1/journals/<id>) に送る (E3-F PR-B1.1)。サーバ側 view (cashbook.new
// / cashbook.edit) は GET 専用で平文 POST は受け付けない。
//
// opts:
//   isEdit, entryId, userId, date, fiscalPeriod, transactionType,
//   paymentAccountCode, categoryAccountCode, amount, description
async function cashbookSubmitE2EE(opts) {
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var sharedClient = new sharedClientMod.SharedCryptoClient(
    "/static/js/crypto/shared-worker.js",
  );
  try {
    var status = await sharedClient.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    var builderMod = await import("/static/js/crypto/entries_builder.js");
    var entry;
    if (opts.transactionType === "transfer") {
      entry = await builderMod.buildTransferEntry({
        client: sharedClient,
        userId: opts.userId,
        date: opts.date,
        description: opts.description || "",
        fromAccountCode: opts.paymentAccountCode,
        toAccountCode: opts.categoryAccountCode,
        amount: opts.amount,
        source: "cashbook",
        fiscalPeriod: opts.fiscalPeriod,
      });
    } else {
      entry = await builderMod.buildCashbookEntry({
        client: sharedClient,
        userId: opts.userId,
        date: opts.date,
        description: opts.description || "",
        transactionType: opts.transactionType,
        paymentAccountCode: opts.paymentAccountCode,
        categoryAccountCode: opts.categoryAccountCode,
        amount: opts.amount,
        source: "cashbook",
        fiscalPeriod: opts.fiscalPeriod,
      });
    }
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";
    var url = opts.isEdit
      ? "/api/v1/journals/" + opts.entryId
      : "/api/v1/journals/batch";
    var method = opts.isEdit ? "PUT" : "POST";
    var body = opts.isEdit ? entry : { entries: [entry] };
    var res = await fetch(url, {
      method: method,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(body),
    });
    var rb = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(rb.error || ("HTTP " + res.status));
    }
    try {
      sessionStorage.setItem(
        "flash:success",
        opts.isEdit ? "伝票を更新しました。" : "伝票を登録しました。",
      );
    } catch (_e) { /* ignore */ }
    window.location.href = "/cashbook/";
  } finally {
    try { sharedClient.close(); } catch (_e) { /* ignore */ }
  }
}
window.cashbookSubmitE2EE = cashbookSubmitE2EE;


// medical/form.html (新規登録) から呼ばれる E2EE submit (E3-F PR-D-3)。
//
// 医療費登録 = (1) 出納帳仕訳 (借方: 医療費科目 / 貸方: 支払元) を batch API に
// 暗号化 POST → 採番された entry id を取得し、(2) MedicalExpense 明細を
// medical-expenses API に暗号化 POST する 2 段。サーバ側 view (medical.new) は
// GET 専用で平文 POST は受け付けない。
//
// opts:
//   userId, medicalAccountCode, paymentAccountCode, date, amountPaid,
//   insuranceReimbursement, patientName, hospitalName, treatmentDescription
async function medicalNewSubmitE2EE(opts) {
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var sharedClient = new sharedClientMod.SharedCryptoClient(
    "/static/js/crypto/shared-worker.js",
  );
  try {
    var status = await sharedClient.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

    // (1) 出納帳仕訳を暗号化して batch API に POST。
    var builderMod = await import("/static/js/crypto/entries_builder.js");
    var entry = await builderMod.buildCashbookEntry({
      client: sharedClient,
      userId: opts.userId,
      date: opts.date,
      description: "医療費: " + (opts.hospitalName || ""),
      transactionType: "expense",
      paymentAccountCode: opts.paymentAccountCode,
      categoryAccountCode: opts.medicalAccountCode,
      amount: opts.amountPaid,
      source: "cashbook",
      fiscalPeriod: null,
    });
    var batchRes = await fetch("/api/v1/journals/batch", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify({ entries: [entry] }),
    });
    var batchBody = await batchRes.json().catch(function() { return {}; });
    if (!batchRes.ok) {
      throw new Error(batchBody.error || ("HTTP " + batchRes.status));
    }
    var entryId = batchBody.entries && batchBody.entries[0]
      ? batchBody.entries[0].id : null;
    if (!entryId) {
      throw new Error("仕訳の作成に失敗しました。");
    }

    // (2) MedicalExpense 明細を暗号化して medical-expenses API に POST。
    var meMod = await import("/static/js/crypto/medical_expense_builder.js");
    var mePayload = await meMod.buildMedicalExpense({
      client: sharedClient,
      userId: opts.userId,
      journalEntryId: entryId,
      date: opts.date,
      patientName: opts.patientName || "",
      hospitalName: opts.hospitalName || "",
      treatmentDescription: opts.treatmentDescription || "",
      providerType: null,
      amountPaid: opts.amountPaid,
      insuranceReimbursement: opts.insuranceReimbursement || 0,
    });
    var meRes = await fetch("/api/v1/medical-expenses", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(mePayload),
    });
    var meBody = await meRes.json().catch(function() { return {}; });
    if (!meRes.ok) {
      throw new Error(meBody.error || ("HTTP " + meRes.status));
    }
    try {
      sessionStorage.setItem("flash:success", "医療費を登録しました。");
    } catch (_e) { /* ignore */ }
    window.location.href = "/medical/";
  } finally {
    try { sharedClient.close(); } catch (_e) { /* ignore */ }
  }
}
window.medicalNewSubmitE2EE = medicalNewSubmitE2EE;


// journal/form.html (新規 + 編集) から呼ばれる E2EE submit。
//
// 仕訳帳 form を JS submit に乗っ取り、N 行可変の lines を entries_builder で
// 暗号化して batch API (新規: POST /api/v1/journals/batch) / PUT API (編集:
// PUT /api/v1/journals/<id>) に送る (E3-F PR-B2)。サーバ側 view (journal.new
// / journal.edit) は GET 専用で平文 POST は受け付けない。
//
// opts:
//   isEdit, entryId, userId, date, fiscalPeriod, description, lines
//   lines: [{account_code, debit_amount, credit_amount, description}, ...]
async function journalSubmitE2EE(opts) {
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var sharedClient = new sharedClientMod.SharedCryptoClient(
    "/static/js/crypto/shared-worker.js",
  );
  try {
    var status = await sharedClient.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    var builderMod = await import("/static/js/crypto/entries_builder.js");
    // serializeLines と同等の正規化: account_code が空 / 両方 0 の行を除外し、
    // form のキー名 (debit_amount/credit_amount) を builder API (debit/credit)
    // に揃える。buildJournalEntry 側でも貸借一致 assert を行う。
    var normalizedLines = [];
    for (var i = 0; i < (opts.lines || []).length; i++) {
      var ln = opts.lines[i];
      var debit = parseInt(ln.debit_amount, 10) || 0;
      var credit = parseInt(ln.credit_amount, 10) || 0;
      if (!ln.account_code) continue;
      if (debit === 0 && credit === 0) continue;
      normalizedLines.push({
        account_code: ln.account_code,
        debit: debit,
        credit: credit,
        description: ln.description || "",
      });
    }
    if (normalizedLines.length < 2) {
      throw new Error("仕訳明細を 2 行以上入力してください。");
    }
    var entry = await builderMod.buildJournalEntry({
      client: sharedClient,
      userId: opts.userId,
      date: opts.date,
      description: opts.description || "",
      lines: normalizedLines,
      source: "journal",
      fiscalPeriod: opts.fiscalPeriod,
    });
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";
    var url = opts.isEdit
      ? "/api/v1/journals/" + opts.entryId
      : "/api/v1/journals/batch";
    var method = opts.isEdit ? "PUT" : "POST";
    var body = opts.isEdit ? entry : { entries: [entry] };
    var res = await fetch(url, {
      method: method,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(body),
    });
    var rb = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(rb.error || ("HTTP " + res.status));
    }
    try {
      sessionStorage.setItem(
        "flash:success",
        opts.isEdit ? "伝票を更新しました。" : "伝票を登録しました。",
      );
    } catch (_e) { /* ignore */ }
    window.location.href = "/journal/";
  } finally {
    try { sharedClient.close(); } catch (_e) { /* ignore */ }
  }
}
window.journalSubmitE2EE = journalSubmitE2EE;


// ai_journal/drafts.html (案 1 で登録ボタン) から呼ばれる E2EE submit (E3-F PR-B3)。
//
// AI 解析済み下書きの案 1 (= suggestions[0]) をクライアント側で暗号化して
// batch API の entry-level draft_id 経路で送信する。サーバは AAD = userId
// のみの暗号文を保存し、commit 内で AIDraft → Voucher 化 + Discord 通知
// 更新を atomic に行う。
//
// opts:
//   draftId, userId, date (YYYY-MM-DD), description, lines
//   lines: [{account_code, debit, credit, description}, ...]
//
// 戻り値: 採番された entry_number (Toast 表示用)
async function aiDraftQuickAcceptE2EE(opts) {
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var sharedClient = new sharedClientMod.SharedCryptoClient(
    "/static/js/crypto/shared-worker.js",
  );
  try {
    var status = await sharedClient.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    var builderMod = await import("/static/js/crypto/entries_builder.js");
    var entry = await builderMod.buildJournalEntry({
      client: sharedClient,
      userId: opts.userId,
      date: opts.date,
      description: opts.description || "",
      lines: opts.lines,
      source: "ai_receipt",
    });
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";
    var body = {
      entries: [Object.assign({}, entry, { draft_id: opts.draftId })],
    };
    var res = await fetch("/api/v1/journals/batch", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(body),
    });
    var rb = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(rb.error || ("HTTP " + res.status));
    }
    var entries = rb.entries || [];
    return entries[0] && entries[0].entry_number;
  } finally {
    try { sharedClient.close(); } catch (_e) { /* ignore */ }
  }
}
window.aiDraftQuickAcceptE2EE = aiDraftQuickAcceptE2EE;


// grouped_accounts (account_selector.html) から {code: name} マップを構築。
// classical.findMatches の相手科目名解決に渡す (サーバ Account.name 相当)。
function _buildAccountNameMap() {
  var map = {};
  var data = window._acctSelectorData;
  if (Array.isArray(data)) {
    for (var i = 0; i < data.length; i++) {
      var accts = (data[i] && data[i].accounts) || [];
      for (var j = 0; j < accts.length; j++) {
        map[accts[j].code] = accts[j].name;
      }
    }
  }
  return map;
}


// CSV 行の日付範囲 (±7 日トレランス込み) が跨ぐ会計年度 (= 暦年) を列挙する。
// classical のマッチングは ±7 日まで遡る/進むため、年初・年末の CSV は隣接
// 年度の仕訳ともマッチしうる。該当する全年度を取得対象にする。
function _reconcileFiscalYears(csvRows) {
  var MS = 86400000;
  var minT = null, maxT = null;
  for (var i = 0; i < csvRows.length; i++) {
    var d = csvRows[i] && csvRows[i].date;
    if (typeof d !== "string") continue;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d);
    if (!m) continue;
    var t = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    if (minT === null || t < minT) minT = t;
    if (maxT === null || t > maxT) maxT = t;
  }
  if (minT === null) return [];
  var startYear = new Date(minT - 7 * MS).getUTCFullYear();
  var endYear = new Date(maxT + 7 * MS).getUTCFullYear();
  var years = [];
  for (var y = startYear; y <= endYear; y++) years.push(y);
  return years;
}


// reconcileMode.loadReconciliation から呼ばれる client 完結照合フロー。
// 年度別に仕訳を取得・復号し classical.findMatches で照合する。
// 戻り値: { result: {csv_results, journal_only, daily_summary},
//          entriesById: {id: 復号済み entry} }  (snap で再暗号化に使う)
async function runReconcileClassical(opts) {
  var classicalMod = await import("/static/js/crypto/reconcile/classical.js");
  var journalsMod = await import("/static/js/crypto/journals_client.js");
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var client = new sharedClientMod.SharedCryptoClient(
    "/static/js/crypto/shared-worker.js",
  );
  try {
    var status = await client.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    var years = _reconcileFiscalYears(opts.csvRows);
    var entries = [];
    for (var i = 0; i < years.length; i++) {
      var yearEntries = await journalsMod.fetchJournalsForYear({
        client: client, userId: opts.userId, fiscalYear: years[i],
      });
      entries = entries.concat(yearEntries);
    }
    var result = classicalMod.findMatches({
      paymentAccountCode: opts.paymentAccountCode,
      csvRows: opts.csvRows,
      journalEntries: entries,
      accountName: _buildAccountNameMap(),
    });
    var entriesById = {};
    for (var j = 0; j < entries.length; j++) {
      entriesById[entries[j].id] = entries[j];
    }
    return { result: result, entriesById: entriesById };
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
window.runReconcileClassical = runReconcileClassical;


// reconcileMode.snapDate から呼ばれる E2EE フロー。
// 復号済み entry を新日付で再暗号化し PUT /api/v1/journals/<id> する
// (旧 snap-date は平文 date のみ更新し blob を放置していた)。
async function snapJournalDateE2EE(opts) {
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var builderMod = await import("/static/js/crypto/entries_builder.js");
  var client = new sharedClientMod.SharedCryptoClient(
    "/static/js/crypto/shared-worker.js",
  );
  try {
    var status = await client.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    var lines = (opts.entry.lines || []).map(function(l) {
      return {
        account_code: l.account_code,
        debit: l.debit || 0,
        credit: l.credit || 0,
        description: l.description || "",
      };
    });
    var built = await builderMod.buildJournalEntry({
      client: client,
      userId: opts.userId,
      date: opts.newDate,
      description: opts.entry.description || "",
      lines: lines,
      source: opts.entry.source || "journal",
      // fiscalPeriod は null にしてサーバに date.month から再判定させる
      // (日付移動で月が変われば期も変わるため)。
      fiscalPeriod: null,
    });
    var csrfMeta = document.querySelector('meta[name="csrf-token"]');
    var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";
    var res = await fetch("/api/v1/journals/" + opts.entryId, {
      method: "PUT",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrfToken,
      },
      body: JSON.stringify(built),
    });
    var rb = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      throw new Error(rb.error || ("HTTP " + res.status));
    }
    return rb.entry_number;
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
window.snapJournalDateE2EE = snapJournalDateE2EE;


// reconcileMode.runAiReconcile から呼ばれる E2EE フロー。
// unmatched / candidates は呼出側が classical の結果から渡す。
async function runReconcileE2EE(opts) {
  opts = opts || {};
  var orchestratorMod = await import("/static/js/crypto/reconcile_orchestrator.js");
  var sharedClientMod = await import("/static/js/crypto/shared-client.js");
  var workerUrl = "/static/js/crypto/shared-worker.js";
  var client = new sharedClientMod.SharedCryptoClient(workerUrl);
  try {
    var status = await client.status();
    if (!status.hasKey) {
      throw new Error("MK ロック中です (設定 → 暗号鍵管理 で解除)");
    }
    return await orchestratorMod.runReconcile({
      client: client,
      unmatched: opts.unmatched || [],
      candidates: opts.candidates || [],
    });
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}
window.runReconcileE2EE = runReconcileE2EE;
