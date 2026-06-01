// analyzeReceiptFull オーケストレーターの Node 単体テスト。

import { test } from "node:test";
import assert from "node:assert/strict";


const ORCH = new URL(
  "../../../app/static/js/crypto/ai_journal_orchestrator.js",
  import.meta.url,
);
const { analyzeReceiptFull } = await import(ORCH.href);


function fakeFile(bytes, type = "image/jpeg") {
  const buf = bytes.buffer ?? bytes;
  return { type, arrayBuffer: async () => buf };
}

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

// E5 (#111): 画像暗号化アップロードは DI で差し替え (実 encrypt/PUT は
// test_ai_upload.mjs で検証)。draftId/aadId を返すだけのフェイク。
function fakeUpload(draftId = 77, aadId = 123n) {
  const fn = async (args) => {
    fn.lastArgs = args;
    return { draftId, aadId, ok: true, status: "pending",
             file_hash_cipher: "ab" };
  };
  return fn;
}

const CTX = {
  round1_prompt: "R1",
  compliance_prompt: "",
  compliance_check_enabled: false,
  round2_prompt_template_no_ledger: "R2_NO __ACCOUNT_LIST_TEXT__",
  round2_prompt_template_with_ledger:
    "R2_W __ACCOUNT_LIST_TEXT__ L __LEDGER_TEXT__",
  account_list_text: "  5010 食費\n  1010 現金",
  custom_prompt: "",
  default_model_by_provider: {
    openai: "gpt-4o",
    anthropic: "claude-sonnet-4-20250514",
    google: "gemini-2.0-flash",
  },
};


test("正常フロー: upload → ai-config → decrypt → prompt-context → R1 → R2 → save", async () => {
  let savePayload = null;
  const fetchImpl = makeFetch([
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o-mini",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/api/v1/ai/prompt-context", () => jsonResp(CTX)],
    [/\/suggestions$/, (init) => {
      savePayload = JSON.parse(init.body);
      return jsonResp({ ok: true, draft: { id: 77 } });
    }],
  ]);
  const uploadEncryptedDraftImpl = fakeUpload(77, 123n);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("sk-test"),
  }));
  let r1Args, r2Args;
  const runRound1Impl = async (args) => {
    r1Args = args;
    return {
      analysis: {
        date: "2026-05-24", description: "セブン", amount: 500,
        document_type: "receipt", items: [],
        needs_ledger: false, requested_accounts: [],
      },
      complianceResult: null,
      usage: { input_tokens: 200, output_tokens: 50 },
      raw: {},
    };
  };
  const runRound2Impl = async (args) => {
    r2Args = args;
    return {
      suggestions: [{ title: "案 1", lines: [
        { account_code: "5010", debit_amount: 500, credit_amount: 0 },
        { account_code: "1010", debit_amount: 0, credit_amount: 500 },
      ]}],
      usage: { input_tokens: 300, output_tokens: 80 },
      raw: {},
      dropped: 0,
      ledgerText: "",
    };
  };

  const ret = await analyzeReceiptFull({
    file: fakeFile(new Uint8Array([1, 2, 3])),
    comment: "テスト",
    client, userId: 9, uploadEncryptedDraftImpl,
    makeThumbnail: async () => new Uint8Array([1]),
    runRound1Impl, runRound2Impl, fetchImpl,
  });

  // 戻り値検証
  assert.equal(ret.draft_id, 77);
  assert.equal(ret.aad_id, 123n);
  assert.equal(ret.provider, "openai");
  // upload は client/userId/file/comment/makeThumbnail を受け取る。
  assert.equal(uploadEncryptedDraftImpl.lastArgs.userId, 9);
  assert.equal(uploadEncryptedDraftImpl.lastArgs.comment, "テスト");
  assert.equal(typeof uploadEncryptedDraftImpl.lastArgs.makeThumbnail, "function");
  assert.equal(ret.model, "gpt-4o-mini");
  assert.equal(ret.suggestions.length, 1);
  assert.equal(ret.analysis.amount, 500);
  // usage 合算
  assert.equal(ret.usage.input_tokens, 500);   // 200 + 300
  assert.equal(ret.usage.output_tokens, 130);  // 50 + 80

  // Round 1 引数
  assert.equal(r1Args.provider, "openai");
  assert.equal(r1Args.apiKey, "sk-test");
  assert.equal(r1Args.model, "gpt-4o-mini");
  assert.equal(r1Args.comment, "テスト");

  // Round 2 引数
  assert.equal(r2Args.round1Analysis.amount, 500);
  assert.equal(r2Args.provider, "openai");
  assert.equal(r2Args.apiKey, "sk-test");

  // save ペイロード
  assert.equal(savePayload.provider, "openai");
  assert.equal(savePayload.model, "gpt-4o-mini");
  assert.equal(savePayload.usage.input_tokens, 500);
  assert.equal(savePayload.suggestions.length, 1);

  // fetch 順序 (画像 upload は DI 化され fetch ではなくなった)
  const urls = fetchImpl.calls.map((c) => c.url);
  assert.equal(urls[0], "/api/v1/ai-config");
  assert.equal(urls[1], "/api/v1/ai/prompt-context");
  assert.match(urls[2], /\/suggestions$/);
});


test("AI config が E2EE 形式でなければ throw", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", is_e2ee: false,
      api_key_blob: null, api_key_iv: null,
    })],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  await assert.rejects(
    () => analyzeReceiptFull({
      file: fakeFile(new Uint8Array(1)), client, userId: 1,
      uploadEncryptedDraftImpl: fakeUpload(), fetchImpl,
      runRound1Impl: async () => ({}), runRound2Impl: async () => ({}),
    }),
    /E2EE 形式ではありません/,
  );
});


test("model_name 空ならデフォルトモデル使用", async () => {
  let r1Args;
  const fetchImpl = makeFetch([
    ["/api/v1/ai-config", () => jsonResp({
      provider: "anthropic", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/api/v1/ai/prompt-context", () => jsonResp(CTX)],
    [/\/suggestions$/, () => jsonResp({ ok: true })],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  const runRound1Impl = async (args) => {
    r1Args = args;
    return { analysis: { needs_ledger: false }, complianceResult: null,
             usage: {}, raw: {} };
  };
  const runRound2Impl = async () => ({
    suggestions: [], usage: {}, raw: {}, dropped: 0, ledgerText: "",
  });
  await analyzeReceiptFull({
    file: fakeFile(new Uint8Array(1)),
    client, userId: 1, uploadEncryptedDraftImpl: fakeUpload(2),
    runRound1Impl, runRound2Impl, fetchImpl,
  });
  assert.equal(r1Args.model, "claude-sonnet-4-20250514");
});


test("default_model_by_provider に provider 未定義なら throw", async () => {
  const fetchImpl = makeFetch([
    ["/api/v1/ai-config", () => jsonResp({
      provider: "evil-provider", model_name: "",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/api/v1/ai/prompt-context", () => jsonResp(CTX)],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => analyzeReceiptFull({
      file: fakeFile(new Uint8Array(1)),
      client, userId: 1, uploadEncryptedDraftImpl: fakeUpload(3), fetchImpl,
      runRound1Impl: async () => ({}), runRound2Impl: async () => ({}),
    }),
    /unsupported provider/,
  );
});


test("画像 upload エラーで早期 reject (ai-config 取得しない)", async () => {
  let aiConfigCalled = false;
  const fetchImpl = makeFetch([
    ["/api/v1/ai-config", () => { aiConfigCalled = true; return jsonResp({}); }],
  ]);
  const client = makeClient(async () => ({ plaintext: new Uint8Array() }));
  const failingUpload = async () => { throw new Error("upload failed: 413"); };
  await assert.rejects(
    () => analyzeReceiptFull({
      file: fakeFile(new Uint8Array(1)), client, userId: 1,
      uploadEncryptedDraftImpl: failingUpload, fetchImpl,
      runRound1Impl: async () => ({}), runRound2Impl: async () => ({}),
    }),
    /upload failed/,
  );
  assert.equal(aiConfigCalled, false);
});


test("Round 1 失敗で reject (Round 2 / save スキップ)", async () => {
  let round2Called = false;
  const fetchImpl = makeFetch([
    ["/api/v1/ai-config", () => jsonResp({
      provider: "openai", model_name: "gpt-4o",
      api_key_blob: "AA==", api_key_iv: "AA==", is_e2ee: true,
    })],
    ["/api/v1/ai/prompt-context", () => jsonResp(CTX)],
  ]);
  const client = makeClient(async () => ({
    plaintext: new TextEncoder().encode("k"),
  }));
  await assert.rejects(
    () => analyzeReceiptFull({
      file: fakeFile(new Uint8Array(1)), client, userId: 1,
      uploadEncryptedDraftImpl: fakeUpload(5), fetchImpl,
      runRound1Impl: async () => { throw new Error("LLM crash"); },
      runRound2Impl: async () => { round2Called = true; return {}; },
    }),
    /LLM crash/,
  );
  assert.equal(round2Called, false);
});


test("required 引数欠如で throw", async () => {
  await assert.rejects(
    () => analyzeReceiptFull({ client: { decrypt: () => {} } }),
    /file is required/,
  );
  await assert.rejects(
    () => analyzeReceiptFull({ file: fakeFile(new Uint8Array(1)) }),
    /client.*is required/,
  );
  // E5 (#111): userId は画像暗号化の AAD 束縛に必須。
  await assert.rejects(
    () => analyzeReceiptFull({
      file: fakeFile(new Uint8Array(1)), client: { decrypt: () => {} },
    }),
    /userId is required/,
  );
});
