// 監査者ダッシュボード 受信表示 (audit_review_renderer) の純粋ロジック (E5 #112)。
// DOM/crypto 非依存の latestRound / parseSnapshot / normalizeEntries を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/audit/audit_review_renderer.mjs",
  import.meta.url,
);
const {
  latestRound,
  parseSnapshot,
  normalizeEntries,
  buildResponseJson,
  validateProposal,
  validateEntryIntegrity,
  validateTrialBalance,
  collectSnapshotIssues,
  summarizeIssues,
  bytesEqual,
} = await import(M.href);

const META = { "1010": { name: "現金" }, "1020": { name: "普通預金" }, "5010": { name: "通信費" } };

test("latestRound: 空なら null", () => {
  assert.equal(latestRound([]), null);
  assert.equal(latestRound(undefined), null);
});

test("latestRound: 最大 round_id のパッケージを返す", () => {
  const pkgs = [
    { id: 1, round_id: 1 },
    { id: 3, round_id: 3 },
    { id: 2, round_id: 2 },
  ];
  assert.equal(latestRound(pkgs).id, 3);
});

test("latestRound: 不正な round_id は無視", () => {
  const pkgs = [{ id: 1, round_id: 2 }, { id: 9, round_id: null }, { id: 8 }];
  assert.equal(latestRound(pkgs).id, 1);
});

test("parseSnapshot: バイト列を JSON へ復元", () => {
  const snap = { v: 1, level: 1, fiscal_year: 2026 };
  const bytes = new TextEncoder().encode(JSON.stringify(snap));
  assert.deepEqual(parseSnapshot(bytes), snap);
});

test("normalizeEntries: Lv1 (集計のみ) は空配列", () => {
  assert.deepEqual(normalizeEntries({ level: 1, trial_balance: [] }), []);
  assert.deepEqual(normalizeEntries(null), []);
});

test("normalizeEntries: Lv2 entries (lines 入れ子) を正規化", () => {
  const snap = {
    level: 2,
    entries: [
      {
        id: 5,
        date: "2026-05-22",
        description: "携帯料金",
        lines: [
          { account_code: "5010", debit: 5000, credit: 0 },
          { account_code: "1010", debit: 0, credit: 5000 },
        ],
      },
    ],
  };
  const out = normalizeEntries(snap);
  assert.equal(out.length, 1);
  assert.equal(out[0].id, 5);
  assert.equal(out[0].description, "携帯料金");
  assert.deepEqual(out[0].lines[0], { account_code: "5010", debit: 5000, credit: 0 });
});

test("normalizeEntries: Lv3 (journal_entries + lines を結合、debit_amount 対応)", () => {
  const snap = {
    level: 3,
    journal_entries: [
      { id: 1, date: "2026-01-01", description: "テスト" },
      { id: 2, date: "2026-02-01", description: "明細なし" },
    ],
    journal_entry_lines: [
      { id: 10, journal_entry_id: 1, account_code: "1010", debit_amount: 100, credit_amount: 0 },
      { id: 11, journal_entry_id: 1, account_code: "4010", debit_amount: 0, credit_amount: 100 },
    ],
  };
  const out = normalizeEntries(snap);
  assert.equal(out.length, 2);
  const e1 = out.find((e) => e.id === 1);
  assert.equal(e1.lines.length, 2);
  // Lv3 は debit_amount/credit_amount を debit/credit へ正規化
  assert.deepEqual(e1.lines[0], { account_code: "1010", debit: 100, credit: 0 });
  // 明細のない仕訳は空配列
  assert.deepEqual(out.find((e) => e.id === 2).lines, []);
});

test("buildResponseJson: revision は summary + comments を含める", () => {
  const out = buildResponseJson({
    responseType: "revision",
    summary: " 全体所見 ",
    comments: [
      { entry_id: 123, ref: " 2026-05-22 携帯料金 ", note: " 貸方は普通預金では？ " },
      { entry_id: null, ref: "", note: "   " }, // note 空はスキップ
    ],
  });
  assert.deepEqual(out, {
    v: 1,
    response_type: "revision",
    summary: "全体所見",
    comments: [{ entry_id: 123, note: "貸方は普通預金では？", ref: "2026-05-22 携帯料金" }],
  });
});

test("buildResponseJson: entry_id 無しの全体指摘は entry_id=null", () => {
  const out = buildResponseJson({
    responseType: "revision",
    comments: [{ note: "全体について" }],
  });
  assert.equal(out.comments.length, 1);
  assert.equal(out.comments[0].entry_id, null);
  assert.equal(out.comments[0].ref, undefined);
  assert.equal(out.summary, undefined);
});

test("buildResponseJson: revision で内容が空なら throw", () => {
  assert.throws(
    () => buildResponseJson({ responseType: "revision", summary: "  ", comments: [] }),
    /1 つ以上/,
  );
});

test("buildResponseJson: rejection は内容空でも可", () => {
  const out = buildResponseJson({ responseType: "rejection" });
  assert.deepEqual(out, { v: 1, response_type: "rejection" });
});

test("buildResponseJson: 未知の response_type は revision に正規化", () => {
  const out = buildResponseJson({ responseType: "bogus", summary: "x" });
  assert.equal(out.response_type, "revision");
});

// ---- §14.9 構造化修正案 (proposal) -------------------------------------

const VALID_PROPOSAL = {
  date: "2026-05-22",
  description: "携帯料金",
  lines: [
    { account_code: "5010", debit: 5000, credit: 0 },
    { account_code: "1020", debit: 0, credit: 5000 },
  ],
};

test("validateProposal: null は null を返す", () => {
  assert.equal(validateProposal(null, META), null);
});

test("validateProposal: 正常系は date/description/lines を正規化", () => {
  const out = validateProposal(VALID_PROPOSAL, META);
  assert.deepEqual(out, {
    date: "2026-05-22",
    description: "携帯料金",
    lines: [
      { account_code: "5010", debit: 5000, credit: 0 },
      { account_code: "1020", debit: 0, credit: 5000 },
    ],
  });
});

test("validateProposal: 文字列金額を整数へ強制 (フォーム値)", () => {
  const out = validateProposal(
    { date: "2026-05-22", lines: [
      { account_code: "5010", debit: "5000", credit: "" },
      { account_code: "1020", debit: "", credit: "5000" },
    ] },
    META,
  );
  assert.equal(out.lines[0].debit, 5000);
  assert.equal(out.lines[1].credit, 5000);
  assert.equal(out.description, "");
});

test("validateProposal: date 必須", () => {
  assert.throws(() => validateProposal({ ...VALID_PROPOSAL, date: "  " }, META), /日付/);
});

test("validateProposal: date は YYYY-MM-DD 形式必須", () => {
  assert.throws(
    () => validateProposal({ ...VALID_PROPOSAL, date: "2026/05/22" }, META),
    /YYYY-MM-DD/,
  );
  assert.throws(
    () => validateProposal({ ...VALID_PROPOSAL, date: "2026-5-2" }, META),
    /YYYY-MM-DD/,
  );
});

test("validateProposal: 行 2 未満は throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [{ account_code: "5010", debit: 5000, credit: 0 }] }, META),
    /2 行以上/,
  );
});

test("validateProposal: 科目未選択は throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "", debit: 5000, credit: 0 },
      { account_code: "1020", debit: 0, credit: 5000 },
    ] }, META),
    /科目を選択/,
  );
});

test("validateProposal: accountsMeta に無い科目は throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "9999", debit: 5000, credit: 0 },
      { account_code: "1020", debit: 0, credit: 5000 },
    ] }, META),
    /存在しません/,
  );
});

test("validateProposal: 非整数・負数の金額は throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "5010", debit: 50.5, credit: 0 },
      { account_code: "1020", debit: 0, credit: 50.5 },
    ] }, META),
    /整数/,
  );
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "5010", debit: -5000, credit: 0 },
      { account_code: "1020", debit: 0, credit: -5000 },
    ] }, META),
    /整数/,
  );
});

test("validateProposal: 借方貸方の両側非ゼロは throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "5010", debit: 5000, credit: 5000 },
      { account_code: "1020", debit: 0, credit: 5000 },
    ] }, META),
    /どちらか一方/,
  );
});

test("validateProposal: 借方貸方の両側ゼロは throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "5010", debit: 0, credit: 0 },
      { account_code: "1020", debit: 0, credit: 5000 },
    ] }, META),
    /どちらか一方/,
  );
});

test("validateProposal: 貸借不一致は throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "5010", debit: 5000, credit: 0 },
      { account_code: "1020", debit: 0, credit: 4000 },
    ] }, META),
    /貸借が一致しません/,
  );
});

test("buildResponseJson: proposal を含む comment を保持", () => {
  const out = buildResponseJson({
    responseType: "revision",
    comments: [{ entry_id: 5, note: "貸方は普通預金では？", proposal: VALID_PROPOSAL }],
    accountsMeta: META,
  });
  assert.equal(out.comments.length, 1);
  assert.equal(out.comments[0].entry_id, 5);
  assert.equal(out.comments[0].note, "貸方は普通預金では？");
  assert.deepEqual(out.comments[0].proposal.lines[1], { account_code: "1020", debit: 0, credit: 5000 });
});

test("buildResponseJson: note 空でも proposal があれば comment を残す", () => {
  const out = buildResponseJson({
    responseType: "revision",
    comments: [{ entry_id: 5, note: "  ", proposal: VALID_PROPOSAL }],
    accountsMeta: META,
  });
  assert.equal(out.comments.length, 1);
  assert.equal(out.comments[0].note, undefined);
  assert.ok(out.comments[0].proposal);
});

test("buildResponseJson: 不正な proposal は throw を伝播", () => {
  assert.throws(
    () => buildResponseJson({
      responseType: "revision",
      comments: [{ entry_id: 5, proposal: { ...VALID_PROPOSAL, date: "" } }],
      accountsMeta: META,
    }),
    /日付/,
  );
});

test("normalizeEntries: is_closing / fiscal_period を伝播 (Lv2)", () => {
  const out = normalizeEntries({
    level: 2,
    entries: [
      { id: 1, date: "2026-05-22", description: "x", is_closing: true, fiscal_period: 16, lines: [] },
      { id: 2, date: "2026-05-22", description: "y", lines: [] },
    ],
  });
  assert.equal(out[0].is_closing, true);
  assert.equal(out[0].fiscal_period, 16);
  assert.equal(out[1].is_closing, false);
  assert.equal(out[1].fiscal_period, null);
});

test("normalizeEntries: is_closing / fiscal_period を伝播 (Lv3)", () => {
  const out = normalizeEntries({
    level: 3,
    journal_entries: [
      { id: 10, date: "2026-05-22", description: "損益振替", is_closing: true, fiscal_period: 16 },
      { id: 11, date: "2026-05-22", description: "通常" },
    ],
    journal_entry_lines: [],
  });
  const e10 = out.find((e) => e.id === 10);
  const e11 = out.find((e) => e.id === 11);
  assert.equal(e10.is_closing, true);
  assert.equal(e10.fiscal_period, 16);
  assert.equal(e11.is_closing, false);
  assert.equal(e11.fiscal_period, null);
});

// ---- validateEntryIntegrity (§12.11 監査時検査) ----------------------------

const _codes = (errs) => errs.map((e) => e.code).sort();

test("validateEntryIntegrity: 正常な貸借一致仕訳 (Lv3) はエラー無し", () => {
  const entry = {
    id: 1, is_closing: false, fiscal_period: 5,
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.deepEqual(errors, []);
});

test("validateEntryIntegrity: Lv3 で貸借不一致を検出", () => {
  const entry = {
    id: 1, lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 99 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(errors).includes("unbalanced"));
});

test("validateEntryIntegrity: 科目マスタに無い account_code を検出", () => {
  const entry = {
    id: 1, lines: [
      { account_code: "9999", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(errors).includes("unknown_account"));
});

test("validateEntryIntegrity: 空の account_code を検出", () => {
  const entry = {
    id: 1, lines: [
      { account_code: "", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(errors).includes("missing_account"));
});

test("validateEntryIntegrity: 借方・貸方両方 > 0 (XOR 違反) を検出", () => {
  const entry = {
    id: 1, lines: [
      { account_code: "5010", debit: 100, credit: 50 },
      { account_code: "1010", debit: 0, credit: 50 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(errors).includes("both_sides"));
});

test("validateEntryIntegrity: 非整数/負の金額を検出", () => {
  const entry = {
    id: 1, lines: [
      { account_code: "5010", debit: 10.5, credit: 0 },
      { account_code: "1010", debit: 0, credit: -3 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(errors).includes("non_integer"));
});

test("validateEntryIntegrity: 手動 period16 (is_closing=false) を検出", () => {
  const entry = {
    id: 1, is_closing: false, fiscal_period: 16,
    lines: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(errors).includes("manual_closing"));
});

test("validateEntryIntegrity: 正規の closing (is_closing=true, period16) は manual_closing 出ない", () => {
  const entry = {
    id: 1, is_closing: true, fiscal_period: 16,
    lines: [
      { account_code: "5010", debit: 0, credit: 100 },
      { account_code: "1010", debit: 100, credit: 0 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(!_codes(errors).includes("manual_closing"));
});

test("validateEntryIntegrity: Lv2 はマスク行で貸借が崩れても unbalanced を出さない", () => {
  // 非公開科目 (proprietor 等) の貸方行がマスク除外され借方のみ残ったケース。
  const entry = {
    id: 1, lines: [
      { account_code: "5010", debit: 100, credit: 0 },
    ],
  };
  const lv2 = validateEntryIntegrity(entry, META, { level: 2 });
  assert.ok(!_codes(lv2.errors).includes("unbalanced"));
  // 同じデータを Lv3 で見ると貸借不一致として検出される。
  const lv3 = validateEntryIntegrity(entry, META, { level: 3 });
  assert.ok(_codes(lv3.errors).includes("unbalanced"));
});

test("validateEntryIntegrity: Lv2 でも可視行の科目存在/XOR は検査する", () => {
  const entry = {
    id: 1, lines: [
      { account_code: "9999", debit: 100, credit: 20 },
    ],
  };
  const { errors } = validateEntryIntegrity(entry, META, { level: 2 });
  const codes = _codes(errors);
  assert.ok(codes.includes("unknown_account"));
  assert.ok(codes.includes("both_sides"));
});

test("validateEntryIntegrity: level 既定は 3 (貸借検査が効く)", () => {
  const entry = {
    id: 1, lines: [{ account_code: "5010", debit: 100, credit: 0 }],
  };
  const { errors } = validateEntryIntegrity(entry, META);
  assert.ok(_codes(errors).includes("unbalanced"));
});

test("validateEntryIntegrity: lines 欠落でも throw せず errors を返す", () => {
  assert.deepEqual(validateEntryIntegrity({}, META, { level: 3 }).errors, []);
  assert.deepEqual(validateEntryIntegrity(null, META, { level: 3 }).errors, []);
});

// ---- validateTrialBalance (§12.11 Lv1 試算表検査) --------------------------

test("validateTrialBalance: 借方合計=貸方合計ならエラー無し", () => {
  const tb = [
    { account_code: "5010", debit: 300, credit: 0 },
    { account_code: "1010", debit: 0, credit: 300 },
  ];
  const r = validateTrialBalance(tb);
  assert.deepEqual(r.errors, []);
  assert.equal(r.debitTotal, 300);
  assert.equal(r.creditTotal, 300);
});

test("validateTrialBalance: 借方≠貸方で trial_unbalanced", () => {
  const tb = [
    { account_code: "5010", debit: 300, credit: 0 },
    { account_code: "1010", debit: 0, credit: 280 },
  ];
  const r = validateTrialBalance(tb);
  assert.ok(r.errors.some((e) => e.code === "trial_unbalanced"));
});

test("validateTrialBalance: 空/非配列は安全に 0 を返す", () => {
  assert.deepEqual(validateTrialBalance([]).errors, []);
  assert.deepEqual(validateTrialBalance(undefined).errors, []);
  assert.equal(validateTrialBalance(null).debitTotal, 0);
});

// ---- collectSnapshotIssues (§12.11 PR-B 集約) ------------------------------

const _SNAP_LV3 = {
  level: 3,
  accounts_meta: META,
  trial_balance: [
    { account_code: "5010", debit: 100, credit: 0 },
    { account_code: "1010", debit: 0, credit: 100 },
  ],
  journal_entries: [
    { id: 1, is_closing: false, fiscal_period: 5 },
    { id: 2, is_closing: false, fiscal_period: 5 },
  ],
  journal_entry_lines: [
    { journal_entry_id: 1, account_code: "5010", debit_amount: 100, credit_amount: 0 },
    { journal_entry_id: 1, account_code: "1010", debit_amount: 0, credit_amount: 100 },
    // 仕訳2: 未知科目 9999 + 貸借不一致 (100 vs 90)
    { journal_entry_id: 2, account_code: "5010", debit_amount: 100, credit_amount: 0 },
    { journal_entry_id: 2, account_code: "9999", debit_amount: 0, credit_amount: 90 },
  ],
};

test("collectSnapshotIssues: Lv3 で仕訳ごとのエラーと件数を集約", () => {
  const issues = collectSnapshotIssues(_SNAP_LV3);
  // 仕訳1 はエラー無し、仕訳2 は unknown_account + unbalanced。
  assert.deepEqual(Object.keys(issues.byEntry), ["2"]);
  assert.equal(issues.counts.unknown_account, 1);
  assert.equal(issues.counts.unbalanced, 1);
  assert.equal(issues.total, 2);
  // trial_balance は釣り合っているので試算表エラー無し。
  assert.deepEqual(issues.trialErrors, []);
});

test("collectSnapshotIssues: クリーンな Lv3 は total 0", () => {
  const clean = {
    level: 3, accounts_meta: META,
    trial_balance: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 100 },
    ],
    journal_entries: [{ id: 1, is_closing: false, fiscal_period: 5 }],
    journal_entry_lines: [
      { journal_entry_id: 1, account_code: "5010", debit_amount: 100, credit_amount: 0 },
      { journal_entry_id: 1, account_code: "1010", debit_amount: 0, credit_amount: 100 },
    ],
  };
  const issues = collectSnapshotIssues(clean);
  assert.equal(issues.total, 0);
  assert.deepEqual(issues.byEntry, {});
  assert.deepEqual(issues.trialErrors, []);
});

test("collectSnapshotIssues: Lv3 は試算表の不一致も仕訳エラーも両方集約", () => {
  const snap = {
    level: 3, accounts_meta: META,
    // 試算表が不一致 (100 vs 90)。
    trial_balance: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 90 },
    ],
    // 仕訳も貸借不一致。
    journal_entries: [{ id: 1, is_closing: false, fiscal_period: 5 }],
    journal_entry_lines: [
      { journal_entry_id: 1, account_code: "5010", debit_amount: 100, credit_amount: 0 },
      { journal_entry_id: 1, account_code: "1010", debit_amount: 0, credit_amount: 90 },
    ],
  };
  const issues = collectSnapshotIssues(snap);
  assert.equal(issues.counts.trial_unbalanced, 1);
  assert.equal(issues.counts.unbalanced, 1);
  assert.equal(issues.trialErrors.length, 1);
  assert.deepEqual(Object.keys(issues.byEntry), ["1"]);
});

test("collectSnapshotIssues: Lv1 (集計のみ) は試算表の不一致のみ検出", () => {
  const lv1 = {
    level: 1, accounts_meta: META,
    trial_balance: [
      { account_code: "5010", debit: 100, credit: 0 },
      { account_code: "1010", debit: 0, credit: 90 },
    ],
  };
  const issues = collectSnapshotIssues(lv1);
  assert.deepEqual(issues.byEntry, {});
  assert.equal(issues.counts.trial_unbalanced, 1);
  assert.equal(issues.trialErrors.length, 1);
});

test("collectSnapshotIssues: Lv2 は試算表/仕訳貸借をスキップし可視行エラーのみ", () => {
  const lv2 = {
    level: 2, accounts_meta: META,
    // Lv2 の試算表はマスクで崩れうるので検査しない。
    trial_balance: [{ account_code: "5010", debit: 100, credit: 0 }],
    entries: [
      // マスクで借方のみ残る = Lv3 なら unbalanced だが Lv2 は計上しない。
      { id: 10, lines: [{ account_code: "5010", debit: 100, credit: 0 }] },
      // 未知科目は Lv2 でも検出。
      { id: 11, lines: [{ account_code: "9999", debit: 50, credit: 0 }] },
    ],
  };
  const issues = collectSnapshotIssues(lv2);
  assert.equal(issues.counts.unbalanced, undefined);
  assert.equal(issues.counts.trial_unbalanced, undefined);
  assert.equal(issues.counts.unknown_account, 1);
  assert.deepEqual(Object.keys(issues.byEntry), ["11"]);
});

// ---- summarizeIssues ---------------------------------------------------

test("summarizeIssues: counts を label 付き配列へ整形し 0 件は除外", () => {
  const out = summarizeIssues({ counts: { unbalanced: 2, unknown_account: 1, non_integer: 0 } });
  const byCode = Object.fromEntries(out.map((x) => [x.code, x]));
  assert.equal(byCode.unbalanced.count, 2);
  assert.equal(byCode.unbalanced.label, "貸借不一致");
  assert.equal(byCode.unknown_account.label, "科目マスタに無い科目");
  assert.equal(byCode.non_integer, undefined); // 0 件は除外
});

test("summarizeIssues: 空/null は空配列", () => {
  assert.deepEqual(summarizeIssues({ counts: {} }), []);
  assert.deepEqual(summarizeIssues(null), []);
});

// ---- bytesEqual (snapshot_hash 照合) -----------------------------------

test("bytesEqual: 同一バイト列は true", () => {
  assert.equal(bytesEqual(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2, 3])), true);
});

test("bytesEqual: 異なる内容/長さ/null は false", () => {
  assert.equal(bytesEqual(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2, 4])), false);
  assert.equal(bytesEqual(new Uint8Array([1, 2]), new Uint8Array([1, 2, 3])), false);
  assert.equal(bytesEqual(null, new Uint8Array([1])), false);
  assert.equal(bytesEqual(new Uint8Array([1]), null), false);
});
