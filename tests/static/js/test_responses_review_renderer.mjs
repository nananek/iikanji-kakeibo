// owner 側 修正案レビュー (responses_review_renderer) の純粋ロジック (E5 #112 / §14.9)。
// DOM/crypto 非依存の responsesForGrant / parseResponse を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/audit/responses_review_renderer.mjs",
  import.meta.url,
);
const { responsesForGrant, parseResponse, computeEntryDiff } = await import(M.href);

test("responsesForGrant: この grant のパッケージの response のみ", () => {
  const responses = [
    { id: 1, audit_package_id: 10, created_at: "2026-06-01T00:00:00Z" },
    { id: 2, audit_package_id: 99, created_at: "2026-06-02T00:00:00Z" }, // 他 grant
    { id: 3, audit_package_id: 11, created_at: "2026-06-03T00:00:00Z" },
  ];
  const out = responsesForGrant(responses, [10, 11]);
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((r) => r.id), [3, 1]); // created_at 降順
});

test("responsesForGrant: パッケージ無しなら空", () => {
  assert.deepEqual(
    responsesForGrant([{ id: 1, audit_package_id: 1 }], []),
    [],
  );
  assert.deepEqual(responsesForGrant(undefined, [1]), []);
});

test("responsesForGrant: created_at 欠落でも安定", () => {
  const responses = [
    { id: 1, audit_package_id: 10 },
    { id: 2, audit_package_id: 10, created_at: "2026-06-02T00:00:00Z" },
  ];
  const out = responsesForGrant(responses, [10]);
  assert.equal(out.length, 2);
  // created_at ありが先頭 (降順)
  assert.equal(out[0].id, 2);
});

test("parseResponse: バイト列を JSON へ復元", () => {
  const payload = {
    v: 1,
    response_type: "revision",
    summary: "所見",
    comments: [{ entry_id: 5, note: "確認" }],
  };
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  assert.deepEqual(parseResponse(bytes), payload);
});

// ---- computeEntryDiff (§14.9 構造化差分) -------------------------------

const OLD_ENTRY = {
  date: "2026-05-22",
  description: "携帯料金",
  lines: [
    { account_code: "5010", debit: 5000, credit: 0, description: "" },
    { account_code: "1010", debit: 0, credit: 5000, description: "" },
  ],
};

test("computeEntryDiff: 科目変更を検出 (changed + fields.account_code)", () => {
  const proposal = {
    date: "2026-05-22",
    description: "携帯料金",
    lines: [
      { account_code: "5010", debit: 5000, credit: 0 },
      { account_code: "1020", debit: 0, credit: 5000 }, // 現金→普通預金
    ],
  };
  const d = computeEntryDiff(OLD_ENTRY, proposal);
  assert.equal(d.date.changed, false);
  assert.equal(d.description.changed, false);
  assert.equal(d.lines[0].status, "unchanged");
  assert.equal(d.lines[1].status, "changed");
  assert.equal(d.lines[1].fields.account_code, true);
  assert.equal(d.lines[1].fields.debit, false);
  assert.equal(d.lines[1].fields.credit, false);
});

test("computeEntryDiff: 金額変更を検出", () => {
  const proposal = {
    date: "2026-05-22",
    description: "携帯料金",
    lines: [
      { account_code: "5010", debit: 6000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 6000 },
    ],
  };
  const d = computeEntryDiff(OLD_ENTRY, proposal);
  assert.equal(d.lines[0].status, "changed");
  assert.equal(d.lines[0].fields.debit, true);
  assert.equal(d.lines[1].fields.credit, true);
});

test("computeEntryDiff: 日付・摘要変更を検出", () => {
  const proposal = {
    date: "2026-05-23",
    description: "通信費",
    lines: OLD_ENTRY.lines,
  };
  const d = computeEntryDiff(OLD_ENTRY, proposal);
  assert.equal(d.date.changed, true);
  assert.equal(d.date.old, "2026-05-22");
  assert.equal(d.date.new, "2026-05-23");
  assert.equal(d.description.changed, true);
  assert.equal(d.description.new, "通信費");
});

test("computeEntryDiff: 行追加 / 削除を検出", () => {
  const added = computeEntryDiff(OLD_ENTRY, {
    date: "2026-05-22", description: "携帯料金",
    lines: [
      ...OLD_ENTRY.lines,
      { account_code: "5020", debit: 100, credit: 0 },
    ],
  });
  assert.equal(added.lines.length, 3);
  assert.equal(added.lines[2].status, "added");
  assert.equal(added.lines[2].old, null);

  const removed = computeEntryDiff(OLD_ENTRY, {
    date: "2026-05-22", description: "携帯料金",
    lines: [OLD_ENTRY.lines[0]],
  });
  assert.equal(removed.lines.length, 2);
  assert.equal(removed.lines[1].status, "removed");
  assert.equal(removed.lines[1].new, null);
});

test("computeEntryDiff: 摘要変更 (行レベル) を検出", () => {
  const proposal = {
    date: "2026-05-22", description: "携帯料金",
    lines: [
      { account_code: "5010", debit: 5000, credit: 0, description: "5月分" },
      { account_code: "1010", debit: 0, credit: 5000 },
    ],
  };
  const d = computeEntryDiff(OLD_ENTRY, proposal);
  assert.equal(d.lines[0].status, "changed");
  assert.equal(d.lines[0].fields.description, true);
});

test("computeEntryDiff: oldEntry が null でも全行 added 扱い", () => {
  const d = computeEntryDiff(null, {
    date: "2026-05-22", description: "x",
    lines: [
      { account_code: "5010", debit: 5000, credit: 0 },
      { account_code: "1010", debit: 0, credit: 5000 },
    ],
  });
  assert.equal(d.lines.every((l) => l.status === "added"), true);
  assert.equal(d.date.changed, true);
});
