// Round 1 (画像→文書解析) JS 実装の Node 単体テスト (E2 PR-C-4b)。

import { test } from "node:test";
import assert from "node:assert/strict";


const ROUND1_URL = new URL(
  "../../../app/static/js/crypto/llm/round1.js",
  import.meta.url,
);
const { runRound1, buildRound1Prompt } = await import(ROUND1_URL.href);


function img(n = 4) {
  return new Uint8Array(n).fill(0xAB);
}


// ============ buildRound1Prompt ============

test("buildRound1Prompt: round1Prompt 必須", () => {
  assert.throws(() => buildRound1Prompt({}), /round1Prompt is required/);
});


test("buildRound1Prompt: 基本 (round1 のみ)", () => {
  const p = buildRound1Prompt({ round1Prompt: "BASE" });
  assert.equal(p, "BASE");
});


test("buildRound1Prompt: compliance_check_enabled=true で compliance_prompt append", () => {
  const p = buildRound1Prompt({
    round1Prompt: "BASE",
    complianceCheckEnabled: true,
    compliancePrompt: "\nCOMPLIANCE",
  });
  assert.equal(p, "BASE\nCOMPLIANCE");
});


test("buildRound1Prompt: compliance disabled なら compliance_prompt は無視", () => {
  const p = buildRound1Prompt({
    round1Prompt: "BASE",
    complianceCheckEnabled: false,
    compliancePrompt: "\nIGNORED",
  });
  assert.equal(p, "BASE");
});


test("buildRound1Prompt: custom_prompt を ## ユーザー定型情報 セクションで append", () => {
  const p = buildRound1Prompt({
    round1Prompt: "BASE",
    customPrompt: "QUICPay=JCB",
  });
  assert(p.includes("## ユーザー定型情報"));
  assert(p.includes("QUICPay=JCB"));
});


test("buildRound1Prompt: comment を「ユーザーからのコメント」で append", () => {
  const p = buildRound1Prompt({
    round1Prompt: "BASE",
    comment: "出張のタクシー代",
  });
  assert(p.includes("ユーザーからのコメント: 出張のタクシー代"));
});


test("buildRound1Prompt: 4 段全て append", () => {
  const p = buildRound1Prompt({
    round1Prompt: "BASE",
    complianceCheckEnabled: true,
    compliancePrompt: "\nC",
    customPrompt: "U",
    comment: "X",
  });
  assert(p.includes("BASE"));
  assert(p.includes("\nC"));
  assert(p.includes("ユーザー定型情報"));
  assert(p.includes("U"));
  assert(p.includes("コメント: X"));
});


// ============ runRound1 ============

const CTX = {
  round1_prompt: "画像を解析してください",
  compliance_prompt: "\n電帳法チェック追加指示",
  compliance_check_enabled: false,
  custom_prompt: "",
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


test("runRound1: 正常 → DocumentAnalysis 整形 + usage 返却", async () => {
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return {
      result: {
        date: "2026-05-24",
        description: "セブン-イレブン",
        amount: 500,
        document_type: "receipt",
        items: [{ name: "コーヒー", amount: 500 }],
        needs_ledger: false,
        requested_accounts: [],
      },
      usage: { input_tokens: 200, output_tokens: 50 },
    };
  };
  const r = await runRound1({
    promptContext: CTX, provider: "openai", apiKey: "k",
    model: "gpt-4o", imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl,
  });
  assert.equal(r.analysis.date, "2026-05-24");
  assert.equal(r.analysis.amount, 500);
  assert.equal(r.analysis.document_type, "receipt");
  assert.deepEqual(r.analysis.items, [{ name: "コーヒー", amount: 500 }]);
  assert.equal(r.analysis.needs_ledger, false);
  assert.deepEqual(r.analysis.requested_accounts, []);
  assert.equal(r.complianceResult, null);
  assert.equal(r.usage.input_tokens, 200);
  // LLM 呼出引数
  assert.equal(llmArgs.provider, "openai");
  assert.equal(llmArgs.apiKey, "k");
  assert.equal(llmArgs.maxTokens, 1000);  // compliance=false なので 1000
  assert.equal(llmArgs.prompt, "画像を解析してください");
});


test("runRound1: compliance_check_enabled=true で maxTokens=1500 + compliance prompt append + complianceResult 整形", async () => {
  const ctx = { ...CTX, compliance_check_enabled: true };
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return {
      result: {
        date: "2026-05-24",
        description: "x",
        amount: 100,
        needs_ledger: false,
        compliance: {
          status: "warn",
          warnings: ["日付が薄い"],
          details: [{ field: "date", reason: "OCR 困難" }],
        },
      },
      usage: { input_tokens: 100, output_tokens: 50 },
    };
  };
  const r = await runRound1({
    promptContext: ctx, provider: "openai", apiKey: "k",
    model: "gpt-4o", imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl,
  });
  // maxTokens は 1500
  assert.equal(llmArgs.maxTokens, 1500);
  // compliance prompt が prompt に含まれる
  assert(llmArgs.prompt.includes("電帳法チェック追加指示"));
  // complianceResult 整形
  assert.equal(r.complianceResult.status, "warn");
  assert.deepEqual(r.complianceResult.warnings, ["日付が薄い"]);
  assert.equal(r.complianceResult.details.length, 1);
});


test("runRound1: compliance status が不正値なら 'pass' に正規化", async () => {
  const ctx = { ...CTX, compliance_check_enabled: true };
  const callLLMImpl = async () => ({
    result: { compliance: { status: "evil", warnings: [], details: [] } },
    usage: {},
  });
  const r = await runRound1({
    promptContext: ctx, provider: "openai", apiKey: "k",
    model: "gpt-4o", imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl,
  });
  assert.equal(r.complianceResult.status, "pass");
});


test("runRound1: custom_prompt + comment が prompt に含まれる", async () => {
  const ctx = { ...CTX, custom_prompt: "QUICPay=JCB" };
  let llmArgs;
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return { result: {}, usage: {} };
  };
  await runRound1({
    promptContext: ctx, provider: "openai", apiKey: "k",
    model: "gpt-4o", imageBytes: img(), mimeType: "image/jpeg",
    comment: "出張",
    callLLMImpl,
  });
  assert(llmArgs.prompt.includes("ユーザー定型情報"));
  assert(llmArgs.prompt.includes("QUICPay=JCB"));
  assert(llmArgs.prompt.includes("コメント: 出張"));
});


test("runRound1: needs_ledger + requested_accounts が抽出される", async () => {
  const callLLMImpl = async () => ({
    result: {
      needs_ledger: true,
      requested_accounts: ["旅費交通費", "通信費"],
    },
    usage: {},
  });
  const r = await runRound1({
    promptContext: CTX, provider: "openai", apiKey: "k",
    model: "gpt-4o", imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl,
  });
  assert.equal(r.analysis.needs_ledger, true);
  assert.deepEqual(r.analysis.requested_accounts, ["旅費交通費", "通信費"]);
});


test("runRound1: LLM が空 / 不正値を返しても DocumentAnalysis にデフォルト値", async () => {
  const callLLMImpl = async () => ({ result: {}, usage: {} });
  const r = await runRound1({
    promptContext: CTX, provider: "openai", apiKey: "k",
    model: "gpt-4o", imageBytes: img(), mimeType: "image/jpeg",
    callLLMImpl,
  });
  assert.equal(r.analysis.date, null);
  assert.equal(r.analysis.description, "");
  assert.equal(r.analysis.amount, 0);
  assert.equal(r.analysis.document_type, "other");
  assert.deepEqual(r.analysis.items, []);
  assert.equal(r.analysis.needs_ledger, false);
});


test("runRound1: promptContext 未指定で throw", async () => {
  await assert.rejects(
    () => runRound1({ provider: "openai", apiKey: "k", model: "m",
      imageBytes: img(), mimeType: "image/jpeg",
      callLLMImpl: async () => ({}) }),
    /promptContext is required/,
  );
});
