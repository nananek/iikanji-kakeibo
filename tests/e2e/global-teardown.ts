import { execSync } from "child_process";

/**
 * モック AI サーバーを停止する。
 */
export default function globalTeardown() {
  try {
    const cmd = process.env.CI
      ? `pkill -f 'mock-ai-server.py' 2>/dev/null || true`
      : `docker compose exec -T web pkill -f 'mock-ai-server.py' 2>/dev/null || true`;
    execSync(cmd, { encoding: "utf-8", timeout: 5000 });
    console.log("global-teardown: mock AI server stopped");
  } catch {
    // プロセスが既に終了している場合は無視
  }
}
