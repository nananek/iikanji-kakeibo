// E2 PR-C-6a: voucher_attach_orchestrator.runVoucherAttachAnalysis テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ORCH = new URL(
  "../../../app/static/js/crypto/voucher_attach_orchestrator.js",
  import.meta.url,
);
const { buildVoucherAttachPrompt, runVoucherAttachAnalysis } =
  await import(ORCH.href);


function jsonResp(body, ok = true, status = 200) {
  return {
    ok, status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function makeFetch(routes) {
  const calls = [];
  const fn = async (url, init = {}) => {
    calls.push({ url, init });
    for (const [pattern, handler] of routes) {
      if (typeof pattern === "string" && url === pattern) return handler(init);
      if (pattern instanceof RegExp && pattern.test(url)) return handler(init);
    }
    throw new Error(`mock: unhandled URL ${url}`);
  };
  fn.calls = calls;
  return fn;
}

function makeClient(decryptFn) {
  return { decrypt: decryptFn };
}

const PROMPT_CTX = {
  prompt_template:
    "DOC\n日付: __JOURNAL_DATE__ 金額: __JOURNAL_AMOUNT__円 摘要: __JOURNAL_DESCRIPTION__",
  compliance_check_enabled: false,
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


// ============ buildVoucherAttachPrompt ============

test("buildVoucherAttachPrompt: 3 プレースホルダを置換", () => {
  const p = buildVoucherAttachPrompt({
    promptTemplate:
      "日付:__JOURNAL_DATE__ 金額:__JOURNAL_AMOUNT__ 摘要:__JOURNAL_DESCRIPTION__",
    journalDate: "2026-02-15",
    journalAmount: 5000,
    journalDescription: "セブン",
  });
  assert.equal(p, "日付:2026-02-15 金額:5000 摘要:セブン");
});

test("buildVoucherAttachPrompt: null/undefined は空文字 or 0", () => {
  const p = buildVoucherAttachPrompt({
    promptTemplate: "[__JOURNAL_DATE__|__JOURNAL_AMOUNT__|__JOURNAL_DESCRIPTION__]",
    journalDate: null,
    journalAmount: undefined,
    journalDescription: null,
  });
  assert.equal(p, "[|0|]");
});

test("buildVoucherAttachPrompt: promptTemplate 欠如で throw", () => {
  assert.throws(
    () => buildVoucherAttachPrompt({ journalDate: "x" }),
    /promptTemplate is required/,
  );
});


// ============ runVoucherAttachAnalysis ============

test("正常フロー: prompt-context + ai-config → decrypt → callLLM → 整形", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    ["/api/v1/voucher-attach/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("sk-test"),
  }));
  const callLLMImpl = async (args) => {
    llmArgs = args;
    return {
      result: {
        consistency: {
          status: "pass",
          date_match: true, amount_match: true, description_match: true,
          warnings: [],
        },
      },
      usage: { input_tokens: 50, output_tokens: 20 },
    };
  };
  const ret = await runVoucherAttachAnalysis({
    imageBytes: new Uint8Array([1, 2, 3]),
    mimeType: "image/jpeg",
    journalDate: "2026-02-15",
    journalAmount: 5000,
    journalDescription: "セブン",
    client, callLLMImpl, fetchImpl,
  });
  assert.equal(ret.consistency.status, "pass");
  assert.equal(ret.consistency.date_match, true);
  assert.equal(ret.compliance, null); // compliance_check_enabled=false
  assert.equal(ret.usage.input_tokens, 50);
  // prompt が組み立てられている
  assert.match(llmArgs.prompt, /2026-02-15/);
  assert.match(llmArgs.prompt, /5000/);
  assert.match(llmArgs.prompt, /セブン/);
  assert.equal(llmArgs.apiKey, "sk-test");
  assert.equal(llmArgs.model, "gpt-4o");
});

test("compliance_check_enabled=true で compliance も返す", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/voucher-attach/prompt-context",
     () => jsonResp({ ...PROMPT_CTX, compliance_check_enabled: true })],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  const ret = await runVoucherAttachAnalysis({
    imageBytes: new Uint8Array([1]),
    mimeType: "image/jpeg",
    journalDate: "2026-02-15", journalAmount: 100, journalDescription: "x",
    client, fetchImpl,
    callLLMImpl: async () => ({
      result: {
        compliance: { status: "warn", warnings: ["影"], details: [] },
        consistency: { status: "pass" },
      },
      usage: {},
    }),
  });
  assert.equal(ret.compliance.status, "warn");
  assert.deepEqual(ret.compliance.warnings, ["影"]);
});

test("consistency 欠如で warn + warnings あり", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/voucher-attach/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  const ret = await runVoucherAttachAnalysis({
    imageBytes: new Uint8Array([1]),
    mimeType: "image/jpeg",
    journalDate: "x", journalAmount: 0, journalDescription: "",
    client, fetchImpl,
    callLLMImpl: async () => ({ result: {}, usage: {} }),
  });
  assert.equal(ret.consistency.status, "warn");
  assert.equal(ret.consistency.date_match, false);
  assert.ok(ret.consistency.warnings.length > 0);
});

test("model_name 空ならデフォルトモデル使用", async () => {
  let llmArgs;
  const fetchImpl = makeFetch([
    ["/api/v1/voucher-attach/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "anthropic", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await runVoucherAttachAnalysis({
    imageBytes: new Uint8Array([1]),
    mimeType: "image/jpeg",
    journalDate: "x", journalAmount: 0, journalDescription: "",
    client, fetchImpl,
    callLLMImpl: async (args) => {
      llmArgs = args;
      return { result: { consistency: { status: "pass" } }, usage: {} };
    },
  });
  assert.equal(llmArgs.model, "claude-sonnet-4-20250514");
});

test("非 E2EE config で throw", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/voucher-attach/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", is_e2ee: false,
      api_key_blob: null, api_key_iv: null,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  await assert.rejects(
    () => runVoucherAttachAnalysis({
      imageBytes: new Uint8Array([1]),
      mimeType: "image/jpeg",
      journalDate: "x", journalAmount: 0, journalDescription: "",
      client, fetchImpl,
      callLLMImpl: async () => ({}),
    }),
    /E2EE 形式ではありません/,
  );
});

test("未対応 provider で throw", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/voucher-attach/prompt-context", () => jsonResp(PROMPT_CTX)],
    ["/api/v1/ai-config", () => jsonResp({
      provider: "evil", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => runVoucherAttachAnalysis({
      imageBytes: new Uint8Array([1]),
      mimeType: "image/jpeg",
      journalDate: "x", journalAmount: 0, journalDescription: "",
      client, fetchImpl,
      callLLMImpl: async () => ({}),
    }),
    /unsupported provider/,
  );
});

test("必須引数欠如で throw", async () => {
  await assert.rejects(
    () => runVoucherAttachAnalysis({
      mimeType: "image/jpeg",
      journalDate: "x", journalAmount: 0, journalDescription: "",
      client: { decrypt: () => {} },
    }),
    /imageBytes must be Uint8Array/,
  );
  await assert.rejects(
    () => runVoucherAttachAnalysis({
      imageBytes: new Uint8Array([1]),
      mimeType: "image/jpeg",
      journalDate: "x", journalAmount: 0, journalDescription: "",
    }),
    /client.*is required/,
  );
});
