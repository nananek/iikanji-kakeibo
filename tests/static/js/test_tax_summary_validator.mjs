// Tests for the pure compareTaxSummary helper.

import { test } from "node:test";
import assert from "node:assert/strict";

const M = new URL(
  "../../../app/static/js/reports/tax_summary_validator.mjs",
  import.meta.url,
);
const { compareTaxSummary } = await import(M.href);


test("完全一致で diffs 空", () => {
  const server = [
    {
      cat: "life_insurance", total: 88370,
      accounts: [{ code: "7010", amount: 88370 }],
    },
    {
      cat: "social_insurance", total: 156590,
      accounts: [
        { code: "6020", amount: 50000 },
        { code: "6030", amount: 90000 },
        { code: "6040", amount: 16590 },
      ],
    },
  ];
  const js = {
    life_insurance: { total: 88370, accounts: [{ code: "7010", amount: 88370 }] },
    social_insurance: {
      total: 156590,
      accounts: [
        { code: "6020", amount: 50000 },
        { code: "6030", amount: 90000 },
        { code: "6040", amount: 16590 },
      ],
    },
  };
  assert.deepEqual(compareTaxSummary(server, js), []);
});

test("カテゴリ total 不一致は category_total_mismatch", () => {
  const server = [{
    cat: "life_insurance", total: 88370,
    accounts: [{ code: "7010", amount: 88370 }],
  }];
  const js = {
    life_insurance: { total: 80000, accounts: [{ code: "7010", amount: 88370 }] },
  };
  const d = compareTaxSummary(server, js);
  assert.ok(d.some((x) => x.kind === "category_total_mismatch"));
});

test("科目 amount 不一致は account_mismatch", () => {
  const server = [{
    cat: "life_insurance", total: 88370,
    accounts: [{ code: "7010", amount: 88370 }],
  }];
  const js = {
    life_insurance: { total: 88370, accounts: [{ code: "7010", amount: 88369 }] },
  };
  const d = compareTaxSummary(server, js);
  assert.ok(d.some((x) => x.kind === "account_mismatch" && x.code === "7010"));
});

test("サーバにあるカテゴリが JS にない → category_missing_in_client", () => {
  const server = [{
    cat: "donation", total: 50000,
    accounts: [{ code: "7040", amount: 50000 }],
  }];
  const js = {};
  const d = compareTaxSummary(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "category_missing_in_client");
  assert.equal(d[0].cat, "donation");
});

test("サーバにある科目が JS にない → account_missing_in_client", () => {
  const server = [{
    cat: "social_insurance", total: 150000,
    accounts: [
      { code: "6020", amount: 100000 },
      { code: "6030", amount: 50000 },
    ],
  }];
  const js = {
    social_insurance: {
      total: 150000,
      accounts: [{ code: "6020", amount: 100000 }],
    },
  };
  const d = compareTaxSummary(server, js);
  assert.ok(d.some((x) => x.kind === "account_missing_in_client" && x.code === "6030"));
});

test("JS にあるカテゴリがサーバにない (非ゼロ) → category_extra_in_client", () => {
  const server = [];
  const js = {
    ideco: { total: 276000, accounts: [{ code: "7060", amount: 276000 }] },
  };
  const d = compareTaxSummary(server, js);
  assert.equal(d.length, 1);
  assert.equal(d[0].kind, "category_extra_in_client");
  assert.equal(d[0].cat, "ideco");
});

test("JS にある科目がサーバにない (非ゼロ) → account_extra_in_client", () => {
  const server = [{
    cat: "social_insurance", total: 100000,
    accounts: [{ code: "6020", amount: 100000 }],
  }];
  const js = {
    social_insurance: {
      total: 100000,
      accounts: [
        { code: "6020", amount: 100000 },
        { code: "6099", amount: 50 },  // 余分
      ],
    },
  };
  const d = compareTaxSummary(server, js);
  assert.ok(d.some((x) => x.kind === "account_extra_in_client" && x.code === "6099"));
});

test("空配列同士は diffs 空", () => {
  assert.deepEqual(compareTaxSummary([], {}), []);
});
