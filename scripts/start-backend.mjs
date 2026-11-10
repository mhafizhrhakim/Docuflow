import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const windows = process.platform === "win32";
const probe = "import fastapi, uvicorn, pymupdf, rapidocr, onnxruntime";

function loadProjectEnvironment() {
  const envFile = path.join(projectRoot, ".env");
  if (!existsSync(envFile)) return;

  for (const line of readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
    if (!match || process.env[match[1]] !== undefined) continue;

    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    } else {
      value = value.replace(/\s+#.*$/, "").trim();
    }
    process.env[match[1]] = value;
  }
}

loadProjectEnvironment();

function pythonCandidates() {
  if (process.env.DOCUFLOW_PYTHON) {
    return [{ command: process.env.DOCUFLOW_PYTHON, args: [], explicit: true }];
  }

  const candidates = [];
  const venvPython = path.join(
    projectRoot,
    windows ? ".venv\\Scripts\\python.exe" : ".venv/bin/python",
  );
  if (existsSync(venvPython)) candidates.push({ command: venvPython, args: [] });
  candidates.push({ command: windows ? "python" : "python3", args: [] });
  if (windows) candidates.push({ command: "py", args: ["-3.11"] });
  return candidates;
}

function environmentCandidates() {
  const environments = [{ env: process.env, label: "interpreter environment" }];
  if (!windows) return environments;

  const sitePaths = [".venv", ".venv_rapidocr"]
    .map((name) => path.join(projectRoot, name, "Lib", "site-packages"))
    .filter(existsSync);
  if (!sitePaths.length) return environments;

  const inherited = process.env.PYTHONPATH
    ? process.env.PYTHONPATH.split(path.delimiter).filter(Boolean)
    : [];
  const pythonPath = [...sitePaths, ...inherited].join(path.delimiter);
  environments.push({
    env: { ...process.env, PYTHONPATH: pythonPath },
    label: "existing workspace site-packages fallback",
  });
  return environments;
}

function canImport(candidate, environment) {
  const result = spawnSync(
    candidate.command,
    [...candidate.args, "-c", probe],
    { cwd: projectRoot, env: environment.env, encoding: "utf8", windowsHide: true },
  );
  return result.status === 0;
}

let selected;
const candidates = pythonCandidates();
const environments = environmentCandidates();
for (const environment of environments) {
  selected = candidates
    .map((candidate) => ({ ...candidate, environment }))
    .find((candidate) => canImport(candidate, environment));
  if (selected) break;
}

if (!selected) {
  const configured = process.env.DOCUFLOW_PYTHON
    ? `DOCUFLOW_PYTHON=${process.env.DOCUFLOW_PYTHON}`
    : "Python 3.11 with FastAPI, Uvicorn, PyMuPDF, RapidOCR, and ONNX Runtime";
  console.error(`DocuFlow backend could not start: ${configured} is unavailable.`);
  console.error("Activate the backend environment or set DOCUFLOW_PYTHON to its interpreter.");
  process.exit(1);
}

console.log(`Starting DocuFlow backend with ${selected.command} (${selected.environment.label}).`);
const appPort = process.env.PORT || process.env.APP_PORT || "8000";
const appEnv = (process.env.APP_ENV || "development").toLowerCase();
const productionRuntime = appEnv === "production" || process.env.RENDER?.toLowerCase() === "true";
const uvicornArgs = ["-m", "uvicorn", "backend.main:app"];
// Uvicorn's reload and worker modes spawn child processes on Windows. Run a
// single process there because socket handoff to those children is unreliable
// in the local Windows runtime. Linux keeps the existing behavior.
if (!windows && !productionRuntime) uvicornArgs.push("--reload");
// Progress and OCR caches are process-local. Keep one worker until these are
// moved to shared storage; threads inside the app already bound PDF jobs.
else if (!windows) uvicornArgs.push("--workers", "1");
const defaultHost = productionRuntime ? "0.0.0.0" : "127.0.0.1";
const configuredHost = process.env.APP_HOST?.trim();
const appHost = productionRuntime && ["127.0.0.1", "localhost"].includes(configuredHost?.toLowerCase())
  ? "0.0.0.0"
  : configuredHost || defaultHost;
uvicornArgs.push("--host", appHost, "--port", appPort);
const backend = spawn(
  selected.command,
  [...selected.args, ...uvicornArgs],
  { cwd: projectRoot, env: selected.environment.env, stdio: "inherit", windowsHide: true },
);

backend.on("error", (error) => {
  console.error(`Failed to launch backend: ${error.message}`);
  process.exitCode = 1;
});
backend.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 1);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (!backend.killed) backend.kill(signal);
  });
}
