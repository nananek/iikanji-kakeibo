// Phase E3-F-3a / Issue #221: 試算表 view 構築の純粋関数。
//
// computeTrialBalance の結果 (account_code → debit/credit) と
// accountsMeta (code → {type, normal_balance, name}) を結合して、
// 科目区分ごとに行を並べた view 用構造を返す。
//
// Issue #221 で opening 入力と grandTotal 出力を追加:
//   - opening は呼出側が BCB から構築した {code: opening_amount} を渡す
//     (期首期間 pf=0 のときは空 dict)
//   - grandTotal は section の subtotal の合計 + balance check
//
// 戻り値:
//   {
//     sections: [
//       {
//         typeCode: "asset"|"liability"|"equity"|"revenue"|"expense",
//         typeName: "資産"|"負債"|"純資産"|"収益"|"費用",
//         rows: [{code, name, opening, debit, credit, balance}],
//         subtotal: {opening, debit, credit, balance},
//       },
//       ...
//     ],
//     grandTotal: {
//       debit, credit, is_balanced  // 借方合計 === 貸方合計
//     },
//   }


const TYPE_ORDER = ["asset", "liability", "equity", "revenue", "expense"];
const TYPE_NAMES = {
  asset: "資産",
  liability: "負債",
  equity: "純資産",
  revenue: "収益",
  expense: "費用",
};


/**
 * 試算表の view 構造を組み立てる純粋関数。
 *
 * @param {Array<{account_code: string, debit: number, credit: number}>} jsRows
 *   computeTrialBalance の戻り値
 * @param {Object<string, {type: string, normal_balance: string, name: string}>} accountsMeta
 *   科目コード → {type, normal_balance, name} のマップ
 * @param {Object} [options]
 * @param {Object<string, number>} [options.opening]
 *   {code: opening_amount} (normal_balance 側を正符号、netted)。指定なし
 *   または該当 code がなければ opening=0 として扱う。
 * @returns {{sections: Array<Object>, grandTotal: Object}}
 */
export function composeTrialBalanceView(jsRows, accountsMeta, options = {}) {
  if (!Array.isArray(jsRows)) {
    throw new TypeError("jsRows must be an array");
  }
  if (!accountsMeta || typeof accountsMeta !== "object") {
    throw new TypeError("accountsMeta must be an object");
  }
  const opening = options.opening || {};

  // 科目区分ごとにバケツ
  const buckets = {};
  for (const t of TYPE_ORDER) {
    buckets[t] = { rows: [], subtotal: { opening: 0, debit: 0, credit: 0, balance: 0 } };
  }

  // opening のみの科目 (期中 0 だが前期繰越あり) も拾うため、jsRows と
  // opening の union で処理する
  const codes = new Set();
  for (const r of jsRows) codes.add(r.account_code);
  for (const code of Object.keys(opening)) codes.add(code);
  const rowsByCode = new Map();
  for (const r of jsRows) rowsByCode.set(r.account_code, r);

  for (const code of codes) {
    const meta = accountsMeta[code];
    if (!meta) continue;
    const typeCode = meta.type;
    if (!(typeCode in buckets)) continue;
    const r = rowsByCode.get(code) || { debit: 0, credit: 0 };
    const isDebitNormal = meta.normal_balance === "debit";
    const op = opening[code] || 0;
    const balance = isDebitNormal
      ? op + r.debit - r.credit
      : op + r.credit - r.debit;
    const row = {
      code,
      name: meta.name || code,
      opening: op,
      debit: r.debit,
      credit: r.credit,
      balance,
    };
    buckets[typeCode].rows.push(row);
    buckets[typeCode].subtotal.opening += op;
    buckets[typeCode].subtotal.debit += r.debit;
    buckets[typeCode].subtotal.credit += r.credit;
    buckets[typeCode].subtotal.balance += balance;
  }

  const sections = [];
  let grandDebit = 0;
  let grandCredit = 0;
  // 残高ベースの貸借一致チェック: 借方科目 (資産 + 費用) の残高合計 ===
  // 貸方科目 (負債 + 純資産 + 収益) の残高合計
  let balanceDebitSide = 0;
  let balanceCreditSide = 0;
  for (const t of TYPE_ORDER) {
    if (buckets[t].rows.length === 0) continue;
    buckets[t].rows.sort((a, b) => a.code.localeCompare(b.code));
    sections.push({
      typeCode: t,
      typeName: TYPE_NAMES[t],
      rows: buckets[t].rows,
      subtotal: buckets[t].subtotal,
    });
    grandDebit += buckets[t].subtotal.debit;
    grandCredit += buckets[t].subtotal.credit;
    if (t === "asset" || t === "expense") {
      balanceDebitSide += buckets[t].subtotal.balance;
    } else {
      balanceCreditSide += buckets[t].subtotal.balance;
    }
  }

  return {
    sections,
    grandTotal: {
      // 合計試算表 (期中借方/貸方取引の総合計)
      debit: grandDebit,
      credit: grandCredit,
      // 残高試算表 (借方科目 vs 貸方科目の残高)
      balance_debit_side: balanceDebitSide,
      balance_credit_side: balanceCreditSide,
      // 残高ベースで一致確認 (期中取引の借方=貸方は仕訳整合性から常に成立
      // するため、check の意味があるのは残高ベースのみ)
      is_balanced: balanceDebitSide === balanceCreditSide,
    },
  };
}
