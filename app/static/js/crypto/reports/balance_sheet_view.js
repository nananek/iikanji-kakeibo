// Phase E3-F-3c: B/S の view 構築純粋関数。
//
// computeBalanceSheet の戻り値を、3 セクション (資産/負債/純資産) +
// 貸借一致チェックの形に整形する。
//
// 戻り値:
//   {
//     sections: {
//       assets:      { rows: [...], total },
//       liabilities: { rows: [...], total },
//       equities:    { rows: [...], total, net_income_row: {balance} | null },
//     },
//     totals: {
//       assets, liabilities, equity_with_ni,
//       diff,           // assets - (liabilities + equity_with_ni)
//       is_balanced,    // diff === 0
//     },
//     has_closing,
//     net_income,
//   }
//
// equity_with_ni は、損益振替前 (has_closing=false) のときは
// total_equity + net_income、振替後は total_equity そのもの。


/**
 * B/S の view を組み立てる。
 *
 * @param {Object} jsResult computeBalanceSheet の戻り値
 * @returns {Object}
 */
export function composeBalanceSheetView(jsResult) {
  if (!jsResult || typeof jsResult !== "object") {
    throw new TypeError("jsResult must be an object");
  }
  for (const k of ["assets", "liabilities", "equities"]) {
    if (!Array.isArray(jsResult[k])) {
      throw new TypeError(`jsResult.${k} must be an array`);
    }
  }

  const hasClosing = !!jsResult.has_closing;
  const netIncome = jsResult.net_income || 0;
  const equityWithNi =
    (jsResult.total_equity || 0) + (hasClosing ? 0 : netIncome);

  const totalAssets = jsResult.total_assets || 0;
  const totalLiabilities = jsResult.total_liabilities || 0;
  const diff = totalAssets - (totalLiabilities + equityWithNi);

  return {
    sections: {
      assets: {
        rows: jsResult.assets.slice(),
        total: totalAssets,
      },
      liabilities: {
        rows: jsResult.liabilities.slice(),
        total: totalLiabilities,
      },
      equities: {
        rows: jsResult.equities.slice(),
        total: equityWithNi,
        // 損益振替前かつ純利益が 0 でない場合のみ "(当期純利益)" 行を表示
        net_income_row: (!hasClosing && netIncome !== 0)
          ? { balance: netIncome }
          : null,
      },
    },
    totals: {
      assets: totalAssets,
      liabilities: totalLiabilities,
      equity_with_ni: equityWithNi,
      diff,
      is_balanced: diff === 0,
    },
    has_closing: hasClosing,
    net_income: netIncome,
  };
}
