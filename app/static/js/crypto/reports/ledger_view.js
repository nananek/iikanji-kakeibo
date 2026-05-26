// Phase E3-F-3e: 元帳 view 構築の純粋関数。
//
// computeLedger の戻り値 + entriesMeta (entry_id → {is_readonly,
// voucher_id, entry_number}) + accountsMeta + sortOrder を結合し、
// 表示用 rows と前期繰越/合計の view 構造を返す。
//
// 戻り値:
//   {
//     opening_balance,
//     rows: [{entry_id, fiscal_period, date, description,
//             counter_account_names, debit, credit, balance,
//             is_readonly, voucher_id, entry_number}],
//     closing_balance, total_debit, total_credit,
//     sort_order: "asc" | "desc",
//   }


function _normalizeSort(s) {
  return s === "desc" ? "desc" : "asc";
}


/**
 * @param {Object} ledgerResult computeLedger の戻り値
 * @param {Object} options
 * @param {Object} options.accountsMeta {[code]: {name, ...}}
 * @param {Object} [options.entriesMeta] {[entry_id]: {is_readonly, voucher_id, entry_number}}
 * @param {"asc"|"desc"} [options.sortOrder="asc"]
 * @returns {Object}
 */
export function composeLedgerView(ledgerResult, options = {}) {
  if (!ledgerResult || typeof ledgerResult !== "object") {
    throw new TypeError("ledgerResult must be an object");
  }
  if (!Array.isArray(ledgerResult.rows)) {
    throw new TypeError("ledgerResult.rows must be an array");
  }
  const accountsMeta = options.accountsMeta || {};
  const entriesMeta = options.entriesMeta || {};
  const sortOrder = _normalizeSort(options.sortOrder);

  // counterparts (comma-separated codes) を names に変換
  function _toNames(codes) {
    if (!codes) return "";
    return codes.split(", ").map((c) => {
      const meta = accountsMeta[c];
      return meta ? (meta.name || c) : c;
    }).join(", ");
  }

  // entriesMeta マージ
  const decoratedRows = ledgerResult.rows.map((r) => {
    const meta = entriesMeta[r.entry_id] || {};
    return {
      entry_id: r.entry_id,
      fiscal_period: r.fiscal_period,
      date: r.date,
      description: r.description || "",
      counter_account_names: _toNames(r.counterparts),
      debit: r.debit || 0,
      credit: r.credit || 0,
      balance: r.balance || 0,
      is_readonly: !!meta.is_readonly,
      voucher_id: meta.voucher_id ?? null,
      entry_number: meta.entry_number ?? null,
    };
  });

  // sort_order = desc なら配列を逆順に
  if (sortOrder === "desc") {
    decoratedRows.reverse();
  }

  return {
    opening_balance: ledgerResult.opening_balance || 0,
    rows: decoratedRows,
    closing_balance: ledgerResult.closing_balance || 0,
    total_debit: ledgerResult.total_debit || 0,
    total_credit: ledgerResult.total_credit || 0,
    sort_order: sortOrder,
  };
}
