import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { resolve } from "node:path";

export function deepseekApiKey({env = process.env, home = homedir()} = {}) {
  if (typeof env.DEEPSEEK_API_KEY === "string" && env.DEEPSEEK_API_KEY.trim()) return env.DEEPSEEK_API_KEY;
  const path = env.SCHOLARFLOW_PI_AUTH_FILE ? resolve(env.SCHOLARFLOW_PI_AUTH_FILE) : resolve(home, ".pi/agent/auth.json");
  try {
    const info = statSync(path);
    if (!info.isFile() || info.nlink !== 1) throw new Error();
    const key = JSON.parse(readFileSync(path, "utf8"))?.deepseek?.key;
    if (typeof key === "string" && key.trim()) return key;
  } catch { /* Return one non-sensitive diagnostic below. */ }
  throw new Error("DeepSeek API credential is not configured or unreadable");
}
