// Round 2 (Round 1 結果 + 元帳 → 仕訳案生成) JS 実装の Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ROUND2_URL = new URL(
  "../../../app/static/js/crypto/llm/round2.js",
  import.meta.url,
);
const {
  buildRound2Prompt, parseAccountCodes,
  runRound2, validateSuggestions,
} = await import(ROUND2_URL.href);


function img(n = 4) {
  return new Uint8Array(n).fill(0xAB);
}


// 共通プロンプトコンテキスト (prompt-context endpoint の戻り値想定)
const CTX = {
  round2_prompt_template_no_ledger:
    "Round 2 PROMPT BASE __ACCOUNT_LIST_TEXT__ END",
  round2_prompt_template_with_ledger:
    "Round 2 PROMPT BASE __ACCOUNT_LIST_TEXT__ LEDGER: __LEDGER_TEXT__ END",
  account_list_text: "[資産]\n  1010 現金\n[費用]\n  5010 食費\n  5020 通信費",
  default_model_by_provider: { openai: "gpt-4o" },
};


// ============ parseAccountCodes ============

test("parseAccountCodes: 行頭の数字コードを Set として抽出", () => {
  const text = "[資産]\n  1010 現金\n  1020 普通預金\n[費用]\n  5010 食費";
  const codes = parseAccountCodes(text);
  assert.equal(codes.size, 3);
  assert(codes.has("1010"));
  assert(codes.has("1020"));
  assert(codes.has("5010"));
});


test("parseAccountCodes: 非文字列で空 Set", () => {
  assert.equal(parseAccountCodes(null).size, 0);
  assert.equal(parseAccountCodes(undefined).size, 0);
});


test("parseAccountCodes: ヘッダ行や空行は無視", () => {
  const text = "[資産]\n\n  1010 現金\n見出しのみ";
  const codes = parseAccountCodes(text);
  assert.equal(codes.size, 1);
  assert(codes.has("1010"));
});


// ============ buildRound2Prompt ============

test("buildRound2Prompt: needs_ledger=false で no_ledger テンプレートを使う", () => {
  const p = buildRound2Prompt({ promptContext: CTX, needsLedger: false });
  assert(p.startsWith("Round 2 PROMPT BASE [資産]"));
  assert(!p.includes("__ACCOUNT_LIST_TEXT__"));
  assert(!p.includes("LEDGER:"));
});


test("buildRound2Prompt: needs_ledger=true で with_ledger テンプレート + LEDGER 置換", () => {
  const p = buildRound2Prompt({
    promptContext: CTX, needsLedger: true,
    ledgerText: "【食費】\n2026-05-23 セブン ¥500",
  });
  assert(p.includes("LEDGER: 【食費】"));
  assert(!p.includes("__LEDGER_TEXT__"));
  assert(!p.includes("__ACCOUNT_LIST_TEXT__"));
});


test("buildRound2Prompt: needs_ledger=true + ledgerText 空でも with_ledger + 空置換", () => {
  const p = buildRound2Prompt({
    promptContext: CTX, needsLedger: true, ledgerText: "",
  });
  assert(p.includes("LEDGER:  END"));  // 空置換
});


test("buildRound2Prompt: promptContext 不足で throw", () => {
  assert.throws(() => buildRound2Prompt({}), /promptContext is required/);
});


test("buildRound2Prompt: テンプレート欠如で throw", () => {
  assert.throws(
    () => buildRound2Prompt({
      promptContext: { account_list_text: "" }, needsLedger: false,
    }),
    /round2_prompt_template_no_ledger/,
  );
});


// ============ validateSuggestions ============

const VALID_CODES = new Set(["1010", "5010", "5020"]);


test("validateSuggestions: 正常 suggestions を整形", () => {
  const raw = [{
    title: "食費案",
    description: "食費として計上",
    date: "2026-05-24",
    entry_description: "セブン",
    lines: [
      { account_code: "5010", account_name: "食費",
        debit_amount: 500, credit_amount: 0 },
      { account_code: "1010", account_name: "現金",
        debit_amount: 0, credit_amount: 500 },
    ],
  }];
  const { suggestions, dropped } = validateSuggestions(raw, VALID_CODES);
  assert.equal(suggestions.length, 1);
  assert.equal(dropped, 0);
  assert.equal(suggestions[0].title, "食費案");
  assert.equal(suggestions[0].lines.length, 2);
});


test("validateSuggestions: 不正 account_code 行を除外", () => {
  const raw = [{
    title: "x",
    lines: [
      { account_code: "9999", debit_amount: 100, credit_amount: 0 },  // 不正
      { account_code: "5010", debit_amount: 100, credit_amount: 0 },
    ],
  }];
  const { suggestions } = validateSuggestions(raw, VALID_CODES);
  // 5010 のみ残る (片側のみで合計不一致だが、validateSuggestions は line 数のみ確認)
  assert.equal(suggestions[0].lines.length, 1);
  assert.equal(suggestions[0].lines[0].account_code, "5010");
});


test("validateSuggestions: 有効 line が 0 件の suggestion は dropped", () => {
  const raw = [
    { title: "全部 invalid", lines: [{ account_code: "9999" }] },
    { title: "valid", lines: [
      { account_code: "5010", debit_amount: 100, credit_amount: 0 },
    ]},
  ];
  const { suggestions, dropped } = validateSuggestions(raw, VALID_CODES);
  assert.equal(suggestions.length, 1);
  assert.equal(dropped, 1);
  assert.equal(suggestions[0].title, "valid");
});


test("validateSuggestions: debit/credit が文字列でも整数に変換", () => {
  const raw = [{
    title: "x",
    lines: [{ account_code: "5010", debit_amount: "100", credit_amount: "0" }],
  }];
  const { suggestions } = validateSuggestions(raw, VALID_CODES);
  assert.equal(suggestions[0].lines[0].debit_amount, 100);
  assert.equal(suggestions[0].lines[0].credit_amount, 0);
});


test("validateSuggestions: 配列でない入力で空配列", () => {
  assert.deepEqual(
    validateSuggestions(null, VALID_CODES),
    { suggestions: [], dropped: 0 },
  );
  assert.deepEqual(
    validateSuggestions("not array", VALID_CODES),
    { suggestions: [], dropped: 0 },
  );
});


// ============ runRound2 (元帳構築 + LLM モック) ============

// 元帳構築用の復号済み仕訳サンプル (fetchJournalsForYear 正規化形式)。
// 食費 (5010) を現金 (1010) で支払った仕訳。
const LEDGER_ENTRIES = [{
  id: 1, date: "2026-05-23", description: "セブン",
  lines: [
    { account_code: "5010", debit: 500, credit: 0 },
    { account_code: "1010", debit: 0, credit: 500 },
  ],
}];


test("runRound2: needs_ledger=false なら元帳構築せず Round 2 のみ", async () => {
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return {
      result: { suggestions: [{ title: "x", lines: [
        { account_code: "5010", debit_amount: 100, credit_amount: 0 },
      ]}]},
      usage: { input_tokens: 200 },
    };
  };
  const r = await runRound2({
    promptContext: CTX,
    round1Analysis: { needs_ledger: false, requested_accounts: [] },
    provider: "openai", apiKey: "k", model: "gpt-4o",
    imageBytes: img(), mimeType: "image/jpeg",
    journalEntries: LEDGER_ENTRIES,
    callLLMImpl,
  });
  assert.equal(r.suggestions.length, 1);
  assert.equal(r.ledgerText, "");
  // プロンプトは no_ledger テンプレート (LEDGER: が含まれない)
  assert(!llmArgs.prompt.includes("LEDGER:"));
});


test("runRound2: needs_ledger=true で journalEntries から元帳構築 + with_ledger テンプレート", async () => {
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return {
      result: { suggestions: [{ title: "x", lines: [
        { account_code: "5010", debit_amount: 100, credit_amount: 0 },
      ]}]},
      usage: {},
    };
  };
  const r = await runRound2({
    promptContext: CTX,
    round1Analysis: {
      needs_ledger: true, requested_accounts: ["食費"],
    },
    provider: "openai", apiKey: "k", model: "gpt-4o",
    imageBytes: img(), mimeType: "image/jpeg",
    journalEntries: LEDGER_ENTRIES,
    callLLMImpl,
  });
  // CTX.account_list_text に "5010 食費" があり、requested_accounts=["食費"] と
  // 部分一致 → 元帳テキストが構築されて prompt に埋め込まれる。
  assert(llmArgs.prompt.includes("LEDGER:"));
  assert(llmArgs.prompt.includes("【食費】（5010）"));
  assert(llmArgs.prompt.includes("セブン"));
  assert(r.ledgerText.includes("【食費】（5010）"));
  // 累計行は廃止 (E3-F PR-D-6-1b)
  assert(!r.ledgerText.includes("累計"));
});


test("runRound2: needs_ledger=true でも該当仕訳なしなら ledgerText 空のまま LLM", async () => {
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return { result: { suggestions: [] }, usage: {} };
  };
  await runRound2({
    promptContext: CTX,
    round1Analysis: {
      // CTX.account_list_text に無い科目名 → 解決されず元帳空
      needs_ledger: true, requested_accounts: ["旅費交通費"],
    },
    provider: "openai", apiKey: "k", model: "gpt-4o",
    imageBytes: img(), mimeType: "image/jpeg",
    journalEntries: LEDGER_ENTRIES,
    callLLMImpl,
  });
  // 空 ledger は no_ledger フローと等価 (LEDGER: ヘッダなし)
  assert(!llmArgs.prompt.includes("LEDGER:"));
});


test("runRound2: journalEntries 未指定でも throw せず元帳空", async () => {
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return { result: { suggestions: [] }, usage: {} };
  };
  await runRound2({
    promptContext: CTX,
    round1Analysis: { needs_ledger: true, requested_accounts: ["食費"] },
    provider: "openai", apiKey: "k", model: "gpt-4o",
    imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl,
  });
  // journalEntries 既定 [] → 元帳空 → LEDGER: なし
  assert(!llmArgs.prompt.includes("LEDGER:"));
});


test("runRound2: account_code バリデーションが効く", async () => {
  const fetchImpl = async () => { throw new Error("nope"); };
  const callLLMImpl = async () => ({
    result: { suggestions: [
      { title: "OK", lines: [
        { account_code: "5010", debit_amount: 100, credit_amount: 0 },
      ]},
      { title: "全 invalid", lines: [
        { account_code: "9999", debit_amount: 100, credit_amount: 0 },
      ]},
    ]},
    usage: {},
  });
  const r = await runRound2({
    promptContext: CTX,
    round1Analysis: { needs_ledger: false, requested_accounts: [] },
    provider: "openai", apiKey: "k", model: "gpt-4o",
    imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl, fetchImpl,
  });
  assert.equal(r.suggestions.length, 1);
  assert.equal(r.dropped, 1);
  assert.equal(r.suggestions[0].title, "OK");
});


test("runRound2: 引数欠如で throw", async () => {
  await assert.rejects(
    () => runRound2({
      round1Analysis: {}, provider: "openai", apiKey: "k", model: "m",
      imageBytes: img(), mimeType: "image/jpeg",
      callLLMImpl: async () => ({}),
    }),
    /promptContext is required/,
  );
  await assert.rejects(
    () => runRound2({
      promptContext: CTX, provider: "openai", apiKey: "k", model: "m",
      imageBytes: img(), mimeType: "image/jpeg",
      callLLMImpl: async () => ({}),
    }),
    /round1Analysis is required/,
  );
});
