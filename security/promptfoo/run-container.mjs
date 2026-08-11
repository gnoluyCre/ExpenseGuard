import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const workdir = resolve(process.cwd());
const dockerPath = workdir.replaceAll("\\", "/");
const image = "ghcr.io/promptfoo/promptfoo:0.122.0";
const args = [
  "run",
  "--rm",
  "--network",
  "none",
  "--read-only",
  "--entrypoint",
  "node",
  "--tmpfs",
  "/tmp:rw,noexec,nosuid,size=64m",
  "--tmpfs",
  "/home/promptfoo/.promptfoo:rw,noexec,nosuid,size=64m,uid=100,gid=101,mode=0700",
  "-e",
  "PROMPTFOO_DISABLE_TELEMETRY=1",
  "-e",
  "PROMPTFOO_DISABLE_UPDATE=1",
  "-e",
  "PROMPTFOO_SELF_HOSTED=true",
  "-v",
  `${dockerPath}:/work:ro`,
  "-w",
  "/work",
  image,
  "/app/dist/src/entrypoint.js",
  "eval",
  "-c",
  "promptfooconfig.yaml",
  "--no-cache",
];
const result = spawnSync("docker", args, { stdio: "inherit", shell: false });
if (result.error) {
  throw result.error;
}
process.exit(result.status ?? 1);
