// E2E 共通ヘルパー (#385 PR#396 §3: 各 spec に重複していた runPython を共通化)。
//
// テストデータの seed や TOTP コード算出などで Python を実行する。CI では直接 `python`、
// ローカルでは `docker compose exec` 経由で web コンテナ内の Python を叩く。

import { spawnSync } from "child_process";

/**
 * web アプリのコンテキストで Python スクリプト (stdin) を実行し stdout を返す。
 * 失敗時 (exit != 0) は stderr を含めて throw する。
 */
export function runPython(stdinScript: string, timeoutMs = 30000): string {
  const [cmd, ...args] = process.env.CI
    ? ["python", "-"]
    : ["docker", "compose", "exec", "-T", "web", "python", "-"];
  const result = spawnSync(cmd, args, {
    input: stdinScript,
    encoding: "utf-8",
    timeout: timeoutMs,
  });
  if (result.status !== 0) {
    throw new Error(`python failed (status=${result.status}): ${result.stderr}`);
  }
  return result.stdout;
}
