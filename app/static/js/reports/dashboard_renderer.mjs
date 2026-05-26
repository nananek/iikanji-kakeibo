// ダッシュボードのクライアント描画。
// MK 復号 → fetchJournalsForYear(year) → composeDashboardView →
// DOM 構築 + Chart.js 月別推移 棒グラフ。


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


function getStaticRoot() {
  return globalThis.IIKANJI_STATIC_ROOT || "/static/";
}


function _fmtYen(n) {
  return "¥" + (n || 0).toLocaleString();
}


function _setStatus(msg, type = "info") {
  const el = document.getElementById("dashboard-status");
  if (!el) return;
  el.textContent = msg;
  el.className = "alert alert-" + type + " py-2";
  el.classList.remove("d-none");
}


function _clearStatus() {
  const el = document.getElementById("dashboard-status");
  if (el) el.classList.add("d-none");
}


function _abort(msg, logArg) {
  if (logArg !== undefined) console.warn("dashboard_renderer: " + msg, logArg);
  _setStatus("ダッシュボードの初期化に失敗しました: " + msg, "danger");
}


function _setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}


function _toggleBalanceCardClass(id, balance) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("text-bg-success", "text-bg-warning");
  el.classList.add(balance >= 0 ? "text-bg-success" : "text-bg-warning");
}


function _renderView(view) {
  _setText("dashboard-monthly-income", _fmtYen(view.monthly.income));
  _setText("dashboard-monthly-expense", _fmtYen(view.monthly.expense));
  _setText("dashboard-monthly-balance", _fmtYen(view.monthly.balance));
  _toggleBalanceCardClass("dashboard-monthly-balance-card", view.monthly.balance);
  _setText("dashboard-yearly-income", _fmtYen(view.yearly.income));
  _setText("dashboard-yearly-expense", _fmtYen(view.yearly.expense));
  _setText("dashboard-yearly-balance", _fmtYen(view.yearly.balance));
}


function _renderChart(trend) {
  if (typeof globalThis.Chart === "undefined") return;
  const canvas = document.getElementById("monthlyChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  new globalThis.Chart(ctx, {
    type: "bar",
    data: {
      labels: trend.map((d) => d.month + "月"),
      datasets: [
        {
          label: "収入",
          data: trend.map((d) => d.income),
          backgroundColor: "rgba(13, 110, 253, 0.7)",
        },
        {
          label: "支出",
          data: trend.map((d) => d.expense),
          backgroundColor: "rgba(220, 53, 69, 0.7)",
        },
      ],
    },
    options: {
      responsive: true,
      scales: {
        y: {
          beginAtZero: true,
          ticks: {
            callback: (v) => "¥" + v.toLocaleString(),
          },
        },
      },
      plugins: {
        tooltip: {
          callbacks: {
            label: (c) => c.dataset.label + ": ¥" + c.parsed.y.toLocaleString(),
          },
        },
      },
    },
  });
}


async function _run() {
  const paramsEl = document.getElementById("dashboard-server-params");
  if (!paramsEl) { _abort("server params script not found"); return; }
  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    _abort("failed to parse params", e);
    return;
  }
  if (typeof params.user_id !== "number" || typeof params.year !== "number"
      || typeof params.month !== "number") {
    _abort("invalid params (user_id/year/month)");
    return;
  }

  const accountsEl = document.getElementById("dashboard-accounts-meta");
  let accountsMeta = {};
  try {
    if (accountsEl) accountsMeta = JSON.parse(accountsEl.textContent);
  } catch (e) {
    _abort("failed to parse accounts meta", e);
    return;
  }

  let client;
  try {
    const [
      { SharedCryptoClient },
      { fetchJournalsForYear },
      { composeDashboardView },
    ] = await Promise.all([
      import(getStaticRoot() + "js/crypto/shared-client.js"),
      import(getStaticRoot() + "js/crypto/journals_client.js"),
      import(getStaticRoot() + "js/crypto/reports/dashboard_view.js"),
    ]);

    client = new SharedCryptoClient(getSharedWorkerUrl());
    const status = await client.status();
    if (!status.hasKey) {
      _setStatus(
        "暗号鍵 (MK) がロックされています。設定 → 暗号鍵管理 で解除してください。",
        "warning",
      );
      return;
    }
    if (params.is_audit_proxy) {
      _setStatus(
        "監査代理閲覧中です。オーナーの暗号化された仕訳はあなたの暗号鍵では復号できないため、ダッシュボードは空表示になります (E2EE アーキテクチャ仕様)。",
        "info",
      );
      return;
    }
    _clearStatus();

    const entries = await fetchJournalsForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const view = composeDashboardView(entries, accountsMeta, {
      month: params.month, untilMonth: params.month,
    });
    _renderView(view);
    _renderChart(view.monthly_trend);
  } catch (e) {
    _setStatus("ダッシュボードの取得に失敗しました: " + (e.message || e), "danger");
  } finally {
    if (client) {
      try { client.close(); } catch (_e) { /* ignore */ }
    }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
