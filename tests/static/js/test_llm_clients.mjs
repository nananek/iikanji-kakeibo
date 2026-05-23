// クライアント側 LLM 呼出ラッパーの Node 単体テスト (E2 PR-C-1)。
//
// fetch は fetchImpl 引数で DI 可能。サーバ呼出を mock し、リクエスト
// 構造の正しさと、レスポンス→{result, usage} の整形を検証する。

import { test } from "node:test";
import assert from "node:assert/strict";


const LLM_URL = new URL(
  "../../../app/static/js/crypto/llm/index.js",
  import.meta.url,
);
const PARSE_URL = new URL(
  "../../../app/static/js/crypto/llm/parse.js",
  import.meta.url,
);

const { callLLM, LLM_HANDLERS } = await import(LLM_URL.href);
const { extractJson, bytesToBase64 } = await import(PARSE_URL.href);


/** Fetch モック生成: provider 別に期待レスポンスを返す。 */
function makeFetchMock(responses) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    const next = responses.shift();
    if (!next) throw new Error("mock: no more responses queued");
    return {
      ok: next.ok ?? true,
      status: next.status ?? 200,
      json: async () => next.body,
      text: async () =>
        typeof next.body === "string" ? next.body : JSON.stringify(next.body),
    };
  };
  fn.calls = calls;
  return fn;
}

function img(n = 4) {
  return new Uint8Array(n).fill(0xAB);
}


// ============ extractJson ============

test("extractJson: 全体が JSON", () => {
  assert.deepEqual(extractJson('{"a":1}'), { a: 1 });
});


test("extractJson: ```json``` ブロック", () => {
  const txt = "前置き\n```json\n{\"x\":2}\n```\n後置き";
  assert.deepEqual(extractJson(txt), { x: 2 });
});


test("extractJson: 最初の {...} を拾う", () => {
  const txt = "前置き { \"y\": 3 } 後置き";
  assert.deepEqual(extractJson(txt), { y: 3 });
});


test("extractJson: 文字列でなければ TypeError", () => {
  assert.throws(() => extractJson(123), /must be string/);
});


test("extractJson: JSON が見つからなければ SyntaxError", () => {
  assert.throws(() => extractJson("plain text"), /no JSON found/);
});


test("bytesToBase64: 既知バイト列", () => {
  // "AB" = 0x41 0x42 → base64 "QUI="
  assert.equal(bytesToBase64(new Uint8Array([0x41, 0x42])), "QUI=");
});


// ============ callLLM (provider 振り分け) ============

test("callLLM: 未知 provider は throw", async () => {
  await assert.rejects(
    () => callLLM({ provider: "evil", apiKey: "x", model: "x", imageBytes: img(), mimeType: "image/jpeg", prompt: "" }),
    /unknown provider: evil/,
  );
});


test("LLM_HANDLERS は openai/anthropic/google を含む + frozen", () => {
  assert.equal(typeof LLM_HANDLERS.openai, "function");
  assert.equal(typeof LLM_HANDLERS.anthropic, "function");
  assert.equal(typeof LLM_HANDLERS.google, "function");
  assert.throws(() => { LLM_HANDLERS.openai = null; }, TypeError);
});


// ============ OpenAI ============

test("OpenAI: 正常 → {result, usage}", async () => {
  const fetchImpl = makeFetchMock([{
    body: {
      choices: [{ message: { content: '{"vendor":"セブン","amount":500}' } }],
      usage: { prompt_tokens: 100, completion_tokens: 30 },
    },
  }]);
  const { result, usage } = await callLLM({
    provider: "openai", apiKey: "sk-x", model: "gpt-4o", imageBytes: img(),
    mimeType: "image/jpeg", prompt: "解析して", fetchImpl,
  });
  assert.deepEqual(result, { vendor: "セブン", amount: 500 });
  assert.equal(usage.input_tokens, 100);
  assert.equal(usage.output_tokens, 30);
  // リクエスト構造の検証
  assert.equal(fetchImpl.calls.length, 1);
  const c = fetchImpl.calls[0];
  assert.equal(c.url, "https://api.openai.com/v1/chat/completions");
  assert.equal(c.init.headers["Authorization"], "Bearer sk-x");
  const body = JSON.parse(c.init.body);
  assert.equal(body.model, "gpt-4o");
  assert.equal(body.messages[0].content[0].text, "解析して");
  assert.match(body.messages[0].content[1].image_url.url, /^data:image\/jpeg;base64,/);
});


test("OpenAI: HTTP エラーで throw", async () => {
  const fetchImpl = makeFetchMock([{
    ok: false, status: 401, body: "Invalid API key",
  }]);
  await assert.rejects(
    () => callLLM({ provider: "openai", apiKey: "sk-bad", model: "gpt-4o",
      imageBytes: img(), mimeType: "image/jpeg", prompt: "", fetchImpl }),
    /OpenAI API error.*401/,
  );
});


test("OpenAI: content がない場合 throw", async () => {
  const fetchImpl = makeFetchMock([{ body: { choices: [{}] } }]);
  await assert.rejects(
    () => callLLM({ provider: "openai", apiKey: "k", model: "m",
      imageBytes: img(), mimeType: "image/jpeg", prompt: "", fetchImpl }),
    /missing content/,
  );
});


// ============ Anthropic ============

test("Anthropic: 正常 → {result, usage} + x-api-key ヘッダ", async () => {
  const fetchImpl = makeFetchMock([{
    body: {
      content: [{ text: '{"vendor":"Apple","amount":1200}' }],
      usage: { input_tokens: 80, output_tokens: 20 },
    },
  }]);
  const { result, usage } = await callLLM({
    provider: "anthropic", apiKey: "sk-ant", model: "claude-3-5-sonnet",
    imageBytes: img(), mimeType: "image/png", prompt: "p", fetchImpl,
  });
  assert.deepEqual(result, { vendor: "Apple", amount: 1200 });
  assert.equal(usage.input_tokens, 80);
  const c = fetchImpl.calls[0];
  assert.equal(c.url, "https://api.anthropic.com/v1/messages");
  assert.equal(c.init.headers["x-api-key"], "sk-ant");
  assert.equal(c.init.headers["anthropic-version"], "2023-06-01");
  // Authorization ヘッダは含まない (Anthropic は x-api-key 形式)
  assert.equal(c.init.headers["Authorization"], undefined);
  const body = JSON.parse(c.init.body);
  assert.equal(body.messages[0].content[0].source.media_type, "image/png");
  assert.equal(body.messages[0].content[1].text, "p");
});


test("Anthropic: HTTP エラーで throw", async () => {
  const fetchImpl = makeFetchMock([{ ok: false, status: 429, body: "rate limit" }]);
  await assert.rejects(
    () => callLLM({ provider: "anthropic", apiKey: "k", model: "m",
      imageBytes: img(), mimeType: "image/jpeg", prompt: "", fetchImpl }),
    /Anthropic API error.*429/,
  );
});


// ============ Google ============

test("Google: 正常 → {result, usage} + key= クエリ", async () => {
  const fetchImpl = makeFetchMock([{
    body: {
      candidates: [{ content: { parts: [{ text: '{"vendor":"スタバ","amount":500}' }] } }],
      usageMetadata: { promptTokenCount: 50, candidatesTokenCount: 15 },
    },
  }]);
  const { result, usage } = await callLLM({
    provider: "google", apiKey: "AIzaXYZ", model: "gemini-1.5-flash",
    imageBytes: img(), mimeType: "image/jpeg", prompt: "p", fetchImpl,
  });
  assert.deepEqual(result, { vendor: "スタバ", amount: 500 });
  assert.equal(usage.input_tokens, 50);
  const c = fetchImpl.calls[0];
  // URL に model + key が埋め込まれる
  assert.match(c.url, /\/gemini-1\.5-flash:generateContent\?key=AIzaXYZ$/);
  const body = JSON.parse(c.init.body);
  assert.equal(body.contents[0].parts[0].text, "p");
  assert.equal(body.contents[0].parts[1].inline_data.mime_type, "image/jpeg");
});


test("Google: model 名は URL encode される", async () => {
  // model 名に / が入ることはないが、安全のため encodeURIComponent
  const fetchImpl = makeFetchMock([{
    body: {
      candidates: [{ content: { parts: [{ text: "{}" }] } }],
      usageMetadata: {},
    },
  }]);
  await callLLM({
    provider: "google", apiKey: "k&special=1", model: "gemini-1.5-flash-002",
    imageBytes: img(), mimeType: "image/jpeg", prompt: "", fetchImpl,
  });
  const c = fetchImpl.calls[0];
  // apiKey の特殊文字が encode される
  assert.match(c.url, /key=k%26special%3D1$/);
});


// ============ 共通バリデーション ============

test("各 provider: apiKey/model/imageBytes 未指定で reject", async () => {
  for (const p of ["openai", "anthropic", "google"]) {
    await assert.rejects(
      () => callLLM({ provider: p, model: "m", imageBytes: img(),
        mimeType: "image/jpeg", prompt: "", fetchImpl: () => {} }),
      /apiKey is required/,
    );
    await assert.rejects(
      () => callLLM({ provider: p, apiKey: "k", imageBytes: img(),
        mimeType: "image/jpeg", prompt: "", fetchImpl: () => {} }),
      /model is required/,
    );
    await assert.rejects(
      () => callLLM({ provider: p, apiKey: "k", model: "m",
        imageBytes: "not bytes", mimeType: "image/jpeg", prompt: "", fetchImpl: () => {} }),
      /imageBytes must be Uint8Array/,
    );
  }
});


test("各 provider: ```json``` ラップされた LLM 応答も extractJson 経由で復元", async () => {
  const wrappedContent = "前置き ```json\n{\"x\":1}\n``` 後置き";
  // OpenAI 形式で wrap
  const fetchImpl = makeFetchMock([{
    body: { choices: [{ message: { content: wrappedContent } }], usage: {} },
  }]);
  const { result } = await callLLM({
    provider: "openai", apiKey: "k", model: "m",
    imageBytes: img(), mimeType: "image/jpeg", prompt: "", fetchImpl,
  });
  assert.deepEqual(result, { x: 1 });
});


test("AbortSignal は fetch に渡される", async () => {
  const fetchImpl = makeFetchMock([{
    body: { choices: [{ message: { content: "{}" } }], usage: {} },
  }]);
  const ctrl = new AbortController();
  await callLLM({
    provider: "openai", apiKey: "k", model: "m",
    imageBytes: img(), mimeType: "image/jpeg", prompt: "",
    signal: ctrl.signal, fetchImpl,
  });
  assert.equal(fetchImpl.calls[0].init.signal, ctrl.signal);
});
