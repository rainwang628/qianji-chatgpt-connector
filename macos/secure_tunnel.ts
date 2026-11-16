/** Store a runtime key in macOS Keychain, or inject it into a fixed tunnel command. */
import { join, resolve } from "node:path";
import { secrets } from "bun";

const root = resolve(import.meta.dir, "..");
const identity = { service: "rain.qianji-readonly.openai-tunnel", name: "runtime-key" };
const action = process.argv[2];

try {
  if (action === "store") {
    const value = (await Bun.stdin.text()).trim();
    if (!value.startsWith("sk-") || value.length < 20 || /\s/.test(value)) {
      throw new Error("invalid key format");
    }
    await secrets.set({ ...identity, value });
    console.log("运行密钥已保存到 macOS 钥匙串。");
  } else if (action === "doctor" || action === "run") {
    const key = await secrets.get(identity);
    if (!key) throw new Error("runtime key missing");
    const args = [join(root, ".runtime/tunnel/tunnel-client"), action,
      "--profile", "qianji-readonly", "--profile-dir", join(root, ".private/tunnel-profiles")];
    if (action === "doctor") args.push("--explain");
    else args.push("--health.url-file", join(root, ".private/tunnel-health.url"));
    const child = Bun.spawn(args, {
      cwd: root,
      env: { ...process.env, OPENAI_API_KEY: undefined, OPENAI_ADMIN_KEY: undefined, CONTROL_PLANE_API_KEY: key },
      stdin: "inherit", stdout: "inherit", stderr: "inherit",
    });
    process.on("SIGTERM", () => child.kill("SIGTERM"));
    process.on("SIGINT", () => child.kill("SIGINT"));
    process.exit(await child.exited);
  } else {
    throw new Error("unsupported action");
  }
} catch {
  console.error("钥匙串操作或隧道启动失败；未显示密钥。请检查本机钥匙串授权与隧道配置。");
  process.exit(1);
}
