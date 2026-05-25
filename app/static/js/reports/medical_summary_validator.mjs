// 医療費控除集計のサーバ集計 (HTML 描画値) とクライアント集計
// (computeMedicalSummary) を並列実行して比較する fail-soft プローブ。
// 表示には影響しない。MK 未ロード時 / 監査代理閲覧時 / 明細ゼロ件時は skip。


export function compareMedicalSummary(serverTotals, serverPatients, jsResult) {
  // serverTotals: {paid, reimbursed, net}
  // serverPatients: [{name, paid, reimbursed, net}]
  // jsResult: { total_paid, total_reimbursed, net_total, by_patient: [...] }
  const diffs = [];

  if (jsResult.total_paid !== serverTotals.paid
      || jsResult.total_reimbursed !== serverTotals.reimbursed
      || jsResult.net_total !== serverTotals.net) {
    diffs.push({
      kind: "totals_mismatch",
      server: serverTotals,
      client: {
        paid: jsResult.total_paid,
        reimbursed: jsResult.total_reimbursed,
        net: jsResult.net_total,
      },
    });
  }

  const byJs = new Map(jsResult.by_patient.map((p) => [p.name, p]));
  for (const sv of serverPatients) {
    const js = byJs.get(sv.name);
    if (!js) {
      if (sv.paid !== 0 || sv.reimbursed !== 0 || sv.net !== 0) {
        diffs.push({ name: sv.name, kind: "patient_missing_in_client" });
      }
      continue;
    }
    if (js.paid !== sv.paid
        || js.reimbursed !== sv.reimbursed
        || js.net !== sv.net) {
      diffs.push({
        name: sv.name, kind: "patient_mismatch",
        server: sv,
        client: { paid: js.paid, reimbursed: js.reimbursed, net: js.net },
      });
    }
  }
  const serverNames = new Set(serverPatients.map((p) => p.name));
  for (const js of jsResult.by_patient) {
    if (!serverNames.has(js.name)
        && (js.paid !== 0 || js.reimbursed !== 0 || js.net !== 0)) {
      diffs.push({
        name: js.name, kind: "patient_extra_in_client",
        client: { paid: js.paid, reimbursed: js.reimbursed, net: js.net },
      });
    }
  }
  return diffs;
}


function getSharedWorkerUrl() {
  return (
    globalThis.IIKANJI_SHARED_WORKER_URL ||
    "/static/js/crypto/shared-worker.js"
  );
}


async function _run() {
  const paramsEl = document.getElementById("medical-summary-server-params");
  if (!paramsEl) return;

  let params;
  try {
    params = JSON.parse(paramsEl.textContent);
  } catch (e) {
    console.warn("medical_summary_validator: failed to parse server params", e);
    return;
  }
  if (params.is_audit_proxy) return;
  if (typeof params.user_id !== "number") return;

  const totalsEl = document.querySelector("[data-medical-summary-totals]");
  if (!totalsEl) return;
  const serverTotals = {
    paid: parseInt(totalsEl.getAttribute("data-server-paid") || "0", 10),
    reimbursed: parseInt(totalsEl.getAttribute("data-server-reimbursed") || "0", 10),
    net: parseInt(totalsEl.getAttribute("data-server-net") || "0", 10),
  };

  // 明細ゼロ件 (合計全 0) なら API 呼ばずに skip
  if (serverTotals.paid === 0 && serverTotals.reimbursed === 0) return;

  const serverPatients = [];
  document.querySelectorAll("[data-medical-patient]").forEach((card) => {
    serverPatients.push({
      name: card.getAttribute("data-medical-patient") || "",
      paid: parseInt(card.getAttribute("data-server-paid") || "0", 10),
      reimbursed: parseInt(card.getAttribute("data-server-reimbursed") || "0", 10),
      net: parseInt(card.getAttribute("data-server-net") || "0", 10),
    });
  });

  const [{ SharedCryptoClient }, { fetchMedicalExpensesForYear }, { computeMedicalSummary }]
    = await Promise.all([
      import("/static/js/crypto/shared-client.js"),
      import("/static/js/crypto/medical_expenses_client.js"),
      import("/static/js/crypto/reports/medical_summary.js"),
    ]);

  const client = new SharedCryptoClient(getSharedWorkerUrl());
  try {
    const status = await client.status();
    if (!status.hasKey) {
      console.info("medical_summary_validator: MK locked, skipping");
      return;
    }
    const expenses = await fetchMedicalExpensesForYear({
      client, userId: params.user_id, fiscalYear: params.year,
    });
    const jsResult = computeMedicalSummary(expenses);
    const diffs = compareMedicalSummary(serverTotals, serverPatients, jsResult);
    if (diffs.length === 0) {
      console.info(
        `%c✓ medical_summary: server vs client 合計 + ${serverPatients.length} 患者 一致`,
        "color: green; font-weight: bold",
      );
    } else {
      console.warn(
        `%c⚠ medical_summary: ${diffs.length} 件の不一致`,
        "color: orange; font-weight: bold",
        diffs,
      );
    }
  } catch (e) {
    console.warn("medical_summary_validator: error", e);
  } finally {
    try { client.close(); } catch (_e) { /* ignore */ }
  }
}


if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _run);
  } else {
    _run();
  }
}
