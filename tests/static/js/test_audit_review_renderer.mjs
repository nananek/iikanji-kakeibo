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

test("validateProposal: 借方貸方の両側非ゼロ/両側ゼロは throw", () => {
  assert.throws(
    () => validateProposal({ date: "2026-05-22", lines: [
      { account_code: "5010", debit: 5000, credit: 5000 },
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
