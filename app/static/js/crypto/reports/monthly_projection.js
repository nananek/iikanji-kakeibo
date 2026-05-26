// Phase E3-F-4e: 当月着地予想 (projection) のクライアントサイド計算。
//
// サーバ `app/services/tax.get_month_projection` の 3 方式
// (pro_rata / rolling28 / dow28) を JS 移植する。
//
// 入力:
//   - comparison view (composeMonthlyComparisonView の戻り値):
//       - income_accounts / expense_accounts: 月別配列 + cost_type
//       - income_totals / expense_totals
//   - entries: fetchJournalsForYear の戻り値 (rolling28 / dow28 用)
//   - options: { method, today (Date), year, month, accountsMeta }
//
// 出力:
//   {
//     month, days_elapsed, days_in_month, method,
//     income_projected: [{name, code, cost_type, actual, projected}],
//     expense_projected: [...],
//     income_total_actual, income_total_projected,
//     expense_total_actual, expense_total_projected,
//   }


const METHODS = new Set(["pro_rata", "rolling28", "dow28"]);


function _daysInMonth(year, monthIdx0) {
  // monthIdx0: 0..11
  return new Date(Date.UTC(year, monthIdx0 + 1, 0)).getUTCDate();
}


/**
 * variable 科目の過去 28 日分の日別発生額マップを作る。
 *
 * 戻り値: { [code]: { [yyyy-mm-dd]: amount } }
 *
 * 過去 28 日が前年に跨る場合、前年分の entries が含まれていれば集計、
 * 含まれていなければ部分集計 (年内分のみ)。
 */
export function collectDailyAmounts28d(entries, accountsMeta, options) {
  if (!Array.isArray(entries)) {
    throw new TypeError("entries must be an array");
  }
  if (!accountsMeta || typeof accountsMeta !== "object") {
    throw new TypeError("accountsMeta must be an object");
  }
  if (!options || !(options.referenceDate instanceof Date)) {
    throw new TypeError("options.referenceDate (Date) is required");
  }
  const refDate = options.referenceDate;
  const startMs = Date.UTC(
    refDate.getUTCFullYear(), refDate.getUTCMonth(), refDate.getUTCDate(),
  ) - 27 * 86400000;
  const endMs = Date.UTC(
    refDate.getUTCFullYear(), refDate.getUTCMonth(), refDate.getUTCDate(),
  );

  const result = {};
  for (const entry of entries) {
    if (entry.source === "closing") continue;
    if (!entry.date) continue;
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(entry.date);
    if (!m) continue;
    const dMs = Date.UTC(+m[1], +m[2] - 1, +m[3]);
    if (dMs < startMs || dMs > endMs) continue;
    for (const line of entry.lines || []) {
      const code = line.account_code;
      if (!code) continue;
      const meta = accountsMeta[code];
      if (!meta) continue;
      if (meta.cost_type !== "variable") continue;
      let net = 0;
      if (meta.type === "expense") {
        net = (line.debit || 0) - (line.credit || 0);
      } else if (meta.type === "revenue") {
        net = (line.credit || 0) - (line.debit || 0);
      } else {
        continue;
      }
      const dateKey = entry.date.slice(0, 10);
      if (!result[code]) result[code] = {};
      result[code][dateKey] = (result[code][dateKey] || 0) + net;
    }
  }
  return result;
}


function _projectProRata(actual, daysInMonth, daysElapsed) {
  if (daysElapsed <= 0) return 0;
  return Math.trunc(actual * daysInMonth / daysElapsed);
}


function _projectRolling28(actual, dailyForCode, today, daysInMonth, daysElapsed) {
  if (!dailyForCode || Object.keys(dailyForCode).length === 0) {
    return _projectProRata(actual, daysInMonth, daysElapsed);
  }
  let total28d = 0;
  for (const v of Object.values(dailyForCode)) total28d += v;
  const dailyAvg = total28d / 28;
  const remainingDays = daysInMonth - daysElapsed;
  return actual + Math.trunc(dailyAvg * remainingDays);
}


function _projectDow28(actual, dailyForCode, today, daysInMonth, daysElapsed) {
  if (!dailyForCode || Object.keys(dailyForCode).length === 0) {
    return _projectProRata(actual, daysInMonth, daysElapsed);
  }
  // 曜日別 (0=月 .. 6=日)
  const dowTotals = [0, 0, 0, 0, 0, 0, 0];
  const yesterdayMs = Date.UTC(
    today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate(),
  ) - 86400000;
  for (let i = 0; i < 28; i++) {
    const dMs = yesterdayMs - i * 86400000;
    const d = new Date(dMs);
    const dateKey =
      d.getUTCFullYear() + "-"
      + String(d.getUTCMonth() + 1).padStart(2, "0") + "-"
      + String(d.getUTCDate()).padStart(2, "0");
    // weekday: JS は日=0, Python は月=0。Python の規約に合わせる
    const jsDow = d.getUTCDay();
    const pyDow = (jsDow + 6) % 7;
    dowTotals[pyDow] += dailyForCode[dateKey] || 0;
  }
  const dowAvg = dowTotals.map((t) => t / 4);

  let remainingSum = 0;
  for (let off = 1; off <= daysInMonth - daysElapsed; off++) {
    const fMs = Date.UTC(
      today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate(),
    ) + off * 86400000;
    const f = new Date(fMs);
    const jsDow = f.getUTCDay();
    const pyDow = (jsDow + 6) % 7;
    remainingSum += dowAvg[pyDow];
  }
  return actual + Math.trunc(remainingSum);
}


/**
 * monthly comparison view + entries + accountsMeta から projection を計算。
 *
 * @param {Object} view  composeMonthlyComparisonView の戻り値
 * @param {Array<Object>} entries  fetchJournalsForYear の戻り値
 * @param {Object} options
 *   - method: "pro_rata" | "rolling28" | "dow28"
 *   - year: number
 *   - month: number (1..12)
 *   - today: Date (UTC ベース推奨)
 *   - accountsMeta: {[code]: {type, cost_type}}
 *   - daysElapsed (任意, 通常 today.getUTCDate())
 * @returns {Object} projection 結構
 */
export function computeProjection(view, entries, options) {
  if (!view || typeof view !== "object") {
    throw new TypeError("view must be an object");
  }
  if (!options || !options.accountsMeta) {
    throw new TypeError("options.accountsMeta is required");
  }
  const method = METHODS.has(options.method) ? options.method : "pro_rata";
  const year = options.year;
  const month = options.month;
  if (!Number.isInteger(year) || !Number.isInteger(month)
      || month < 1 || month > 12) {
    throw new TypeError("options.year / options.month invalid");
  }
  const today = options.today instanceof Date ? options.today : new Date();
  const daysInMonth = _daysInMonth(year, month - 1);
  const daysElapsed = options.daysElapsed ?? today.getUTCDate();

  let daily = {};
  if (method !== "pro_rata" && daysElapsed > 0) {
    const yesterdayMs = Date.UTC(
      today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate(),
    ) - 86400000;
    daily = collectDailyAmounts28d(entries, options.accountsMeta, {
      referenceDate: new Date(yesterdayMs),
    });
  }

  const mIdx = month - 1;
  const prevIdx = mIdx - 1;

  function _project(accounts) {
    return accounts.map((a) => {
      const actual = a.months[mIdx] || 0;
      const ct = a.cost_type || "occasional";
      let projected;
      if (ct === "fixed") {
        const prev = prevIdx >= 0 ? (a.months[prevIdx] || 0) : 0;
        projected = prev > 0 ? prev : actual;
      } else if (ct === "variable") {
        if (method === "rolling28") {
          projected = _projectRolling28(
            actual, daily[a.code], today, daysInMonth, daysElapsed,
          );
        } else if (method === "dow28") {
          projected = _projectDow28(
            actual, daily[a.code], today, daysInMonth, daysElapsed,
          );
        } else {
          projected = _projectProRata(actual, daysInMonth, daysElapsed);
        }
      } else {
        projected = actual;
      }
      return {
        name: a.name, code: a.code,
        cost_type: a.cost_type, actual, projected,
      };
    });
  }

  const income_projected = _project(view.income_accounts);
  const expense_projected = _project(view.expense_accounts);

  const sumProjected = (arr) => arr.reduce((s, p) => s + p.projected, 0);

  return {
    month, days_elapsed: daysElapsed, days_in_month: daysInMonth, method,
    income_projected, expense_projected,
    income_total_actual: view.income_totals[mIdx] || 0,
    income_total_projected: sumProjected(income_projected),
    expense_total_actual: view.expense_totals[mIdx] || 0,
    expense_total_projected: sumProjected(expense_projected),
  };
}
