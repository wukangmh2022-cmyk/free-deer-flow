const { app, BrowserWindow, dialog, Menu } = require("electron");
const path = require("path");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");

const IS_WINDOWS = process.platform === "win32";
const USER_HOME =
  process.env.HOME ||
  process.env.USERPROFILE ||
  (process.env.HOMEDRIVE && process.env.HOMEPATH ? `${process.env.HOMEDRIVE}${process.env.HOMEPATH}` : "");

const resolveRepoRoot = () => {
  const devRoot = path.resolve(__dirname, "..", "..");
  if (!app.isPackaged) {
    return devRoot;
  }
  const packagedCandidates = [
    path.join(process.resourcesPath, "runtime"),
    path.join(process.resourcesPath, "app", "runtime"),
    devRoot
  ];
  for (const candidate of packagedCandidates) {
    if (
      fs.existsSync(path.join(candidate, "backend")) &&
      (fs.existsSync(path.join(candidate, "scripts")) || fs.existsSync(path.join(candidate, "frontend-static")))
    ) {
      return candidate;
    }
  }
  return packagedCandidates[0];
};

const REPO_ROOT = resolveRepoRoot();
const BACKEND_DIR = path.join(REPO_ROOT, "backend");
const DEFAULT_DESKTOP_DATA_DIR = app.isPackaged
  ? path.join(process.resourcesPath, "runtime-data")
  : path.join(BACKEND_DIR, ".deer-flow");
const PORTABLE_DATA_DIR =
  process.env.DEER_FLOW_HOME || DEFAULT_DESKTOP_DATA_DIR;
const USE_STATIC_FRONTEND =
  app.isPackaged || process.env.DEER_FLOW_USE_STATIC_FRONTEND === "1";
const FRONTEND_STATIC_DIR = app.isPackaged
  ? path.join(REPO_ROOT, "frontend-static")
  : path.join(REPO_ROOT, "desktop", "electron", "runtime", "frontend-static");
const STATIC_SERVER_ENTRY = app.isPackaged
  ? path.join(process.resourcesPath, "app.asar", "static-server.cjs")
  : path.join(REPO_ROOT, "desktop", "electron", "static-server.cjs");
const RUNTIME_PYTHON_DIR = path.join(REPO_ROOT, "python");
const PLAYWRIGHT_BROWSERS_DIR = path.join(RUNTIME_PYTHON_DIR, "ms-playwright");
const RUNTIME_BUILD_INFO_PATH = path.join(RUNTIME_PYTHON_DIR, "runtime-build.json");
const RUNTIME_PYTHON_EXE_CANDIDATES = IS_WINDOWS
  ? [
      path.join(RUNTIME_PYTHON_DIR, "python.exe"),
      path.join(RUNTIME_PYTHON_DIR, "venv", "Scripts", "python.exe"),
      path.join(RUNTIME_PYTHON_DIR, "Scripts", "python.exe")
    ]
  : [
      path.join(RUNTIME_PYTHON_DIR, "bin", "python3"),
      path.join(RUNTIME_PYTHON_DIR, "venv", "bin", "python3"),
      path.join(RUNTIME_PYTHON_DIR, "python3")
    ];
const RUNTIME_CONFIG_PATH = path.join(REPO_ROOT, "config.yaml");
const FALLBACK_CONFIG_PATH = path.join(REPO_ROOT, "config.example.yaml");
const LOG_DIR = app.isPackaged
  ? path.join(USER_HOME || process.cwd(), ".deerflowwithdeepseek", "logs")
  : path.join(REPO_ROOT, "logs");

const PROVIDER_HOST = process.env.DEEPSEEK_LOCAL_PROVIDER_HOST || "127.0.0.1";
const PROVIDER_PORT = Number(process.env.DEEPSEEK_LOCAL_PROVIDER_PORT || "8765");
const WEB_PORT = Number(process.env.DEER_FLOW_WEB_PORT || (app.isPackaged ? "3000" : "2026"));
const DESKTOP_PROFILE = (process.env.DEER_FLOW_DESKTOP_PROFILE || "dev").toLowerCase();
const DEFAULT_MODEL = process.env.DEEPSEEK_LOCAL_MODEL || "DeepSeekV4";
const SHOULD_SKIP_INSTALL = !app.isPackaged;
const PACKAGED_INSTALL_TIMEOUT_MS = IS_WINDOWS ? 600000 : 240000;
const GATEWAY_PORT = Number(process.env.DEER_FLOW_GATEWAY_PORT || "8001");
const FRONTEND_MAX_OLD_SPACE_MB = Number(process.env.DEER_FLOW_FRONTEND_MAX_OLD_SPACE_MB || (app.isPackaged ? "384" : "0"));

let mainWindow = null;
let providerProc = null;
let gatewayProc = null;
let frontendProc = null;
let serveProc = null;
let quitting = false;
const APP_URL = `http://127.0.0.1:${WEB_PORT}/`;
const PROVIDER_LOGIN_MODELS = {
  deepseek: "deepseek-web-deerflow",
  xiaomi: "xiaomi-mimo-v2-pro"
};
const DEFAULT_EXTENSIONS_CONFIG = `${JSON.stringify(
  {
    mcpServers: {},
    skills: {}
  },
  null,
  2
)}\n`;

const readRuntimeBuildInfo = () => {
  if (!fs.existsSync(RUNTIME_BUILD_INFO_PATH)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(RUNTIME_BUILD_INFO_PATH, "utf8"));
  } catch {
    return null;
  }
};

const RUNTIME_BUILD_INFO = readRuntimeBuildInfo();
const PLAYWRIGHT_BROWSER_MODE =
  process.env.DEER_FLOW_PLAYWRIGHT_BROWSER_MODE ||
  (RUNTIME_BUILD_INFO && RUNTIME_BUILD_INFO.playwrightBrowserMode) ||
  "bundled";

const logLine = (msg) => {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(path.join(LOG_DIR, "electron_desktop.log"), `[${new Date().toISOString()}] ${msg}\n`);
  } catch {
    // no-op
  }
};

const getDesktopExtensionsConfigPath = () =>
  process.env.DEER_FLOW_EXTENSIONS_CONFIG_PATH ||
  path.join(PORTABLE_DATA_DIR, "extensions_config.json");

const ensureDesktopExtensionsConfig = () => {
  const targetPath = getDesktopExtensionsConfigPath();
  const targetDir = path.dirname(targetPath);

  fs.mkdirSync(PORTABLE_DATA_DIR, { recursive: true });
  fs.mkdirSync(targetDir, { recursive: true });

  if (fs.existsSync(targetPath)) {
    return targetPath;
  }

  const repoConfigPath = path.join(REPO_ROOT, "extensions_config.json");
  if (fs.existsSync(repoConfigPath)) {
    fs.copyFileSync(repoConfigPath, targetPath);
    logLine(`seeded desktop extensions config from repo config: ${targetPath}`);
    return targetPath;
  }

  fs.writeFileSync(targetPath, DEFAULT_EXTENSIONS_CONFIG, "utf8");
  logLine(`created desktop extensions config: ${targetPath}`);
  return targetPath;
};

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
}

const commandExists = (command) => {
  const checker = IS_WINDOWS ? "where" : "which";
  const result = spawnSync(checker, [command], { stdio: "ignore", shell: true });
  return result.status === 0;
};

const getBundledPythonPath = () =>
  RUNTIME_PYTHON_EXE_CANDIDATES.find((candidate) => fs.existsSync(candidate));

const hasBundledPython = () => app.isPackaged && Boolean(getBundledPythonPath());

const getBundledPlaywrightBrowserDir = () => {
  if (!app.isPackaged) {
    return null;
  }
  if (!fs.existsSync(PLAYWRIGHT_BROWSERS_DIR)) {
    return null;
  }
  const candidates = fs
    .readdirSync(PLAYWRIGHT_BROWSERS_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith("chromium-"))
    .map((entry) => path.join(PLAYWRIGHT_BROWSERS_DIR, entry.name));
  return candidates[0] || null;
};

const detectSystemPlaywrightBrowser = () => {
  const explicitExecutablePath = process.env.DEER_FLOW_PLAYWRIGHT_EXECUTABLE_PATH;
  if (explicitExecutablePath && fs.existsSync(explicitExecutablePath)) {
    return {
      kind: "custom",
      executablePath: explicitExecutablePath,
      channel: process.env.DEER_FLOW_PLAYWRIGHT_BROWSER_CHANNEL || undefined
    };
  }

  const explicitChannel = process.env.DEER_FLOW_PLAYWRIGHT_BROWSER_CHANNEL;
  if (explicitChannel) {
    return {
      kind: explicitChannel,
      channel: explicitChannel
    };
  }

  if (IS_WINDOWS) {
    const localAppData = process.env.LOCALAPPDATA || path.join(USER_HOME, "AppData", "Local");
    const programFiles = process.env.ProgramFiles || "C:\\Program Files";
    const programFilesX86 = process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
    const candidates = [
      {
        kind: "msedge",
        channel: "msedge",
        executablePath: path.join(programFilesX86, "Microsoft", "Edge", "Application", "msedge.exe")
      },
      {
        kind: "msedge",
        channel: "msedge",
        executablePath: path.join(programFiles, "Microsoft", "Edge", "Application", "msedge.exe")
      },
      {
        kind: "msedge",
        channel: "msedge",
        executablePath: path.join(localAppData, "Microsoft", "Edge", "Application", "msedge.exe")
      },
      {
        kind: "chrome",
        channel: "chrome",
        executablePath: path.join(programFiles, "Google", "Chrome", "Application", "chrome.exe")
      },
      {
        kind: "chrome",
        channel: "chrome",
        executablePath: path.join(programFilesX86, "Google", "Chrome", "Application", "chrome.exe")
      },
      {
        kind: "chrome",
        channel: "chrome",
        executablePath: path.join(localAppData, "Google", "Chrome", "Application", "chrome.exe")
      }
    ];
    return candidates.find((candidate) => fs.existsSync(candidate.executablePath)) || null;
  }

  if (process.platform === "darwin") {
    const candidates = [
      {
        kind: "msedge",
        channel: "msedge",
        executablePath: "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
      },
      {
        kind: "chrome",
        channel: "chrome",
        executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      }
    ];
    return candidates.find((candidate) => fs.existsSync(candidate.executablePath)) || null;
  }

  return null;
};

const getSystemPlaywrightBrowser = () => detectSystemPlaywrightBrowser();

const pythonCommandExists = () => commandExists("python3") || commandExists("python");

const resolvePythonRuntime = () => {
  const bundledPython = getBundledPythonPath();
  if (app.isPackaged && bundledPython) {
    return {
      kind: "bundled-python",
      command: bundledPython,
      baseArgs: []
    };
  }

  if (commandExists("uv")) {
    return {
      kind: "uv",
      command: "uv",
      baseArgs: ["run"]
    };
  }

  if (commandExists("python3")) {
    return {
      kind: "python3",
      command: "python3",
      baseArgs: []
    };
  }

  if (commandExists("python")) {
    return {
      kind: "python",
      command: "python",
      baseArgs: []
    };
  }

  return null;
};

const pythonModuleArgs = (runtime, moduleName, moduleArgs = []) => {
  if (!runtime) {
    throw new Error("No Python runtime available.");
  }
  if (runtime.kind === "uv") {
    return [...runtime.baseArgs, moduleName, ...moduleArgs];
  }
  return [...runtime.baseArgs, "-m", moduleName, ...moduleArgs];
};

const collectStartupProblems = () => {
  const problems = [];
  const requiredPaths = [["repo root", REPO_ROOT], ["backend dir", BACKEND_DIR]];
  if (USE_STATIC_FRONTEND) {
    requiredPaths.push(["frontend static dir", FRONTEND_STATIC_DIR]);
    requiredPaths.push(["static server entry", STATIC_SERVER_ENTRY]);
  } else {
    requiredPaths.push(["scripts dir", path.join(REPO_ROOT, "scripts")]);
    requiredPaths.push(["serve.sh", path.join(REPO_ROOT, "scripts", "serve.sh")]);
    if (IS_WINDOWS) {
      requiredPaths.push(["run-with-git-bash.cmd", path.join(REPO_ROOT, "scripts", "run-with-git-bash.cmd")]);
    }
  }
  for (const [name, p] of requiredPaths) {
    if (!fs.existsSync(p)) {
      problems.push(`missing ${name}: ${p}`);
    }
  }

  if (!resolvePythonRuntime()) {
    problems.push(
      `no Python runtime found: expected bundled runtime at one of ${RUNTIME_PYTHON_EXE_CANDIDATES.join(", ")} or one of uv/python3/python on PATH`
    );
  }

  if (app.isPackaged && PLAYWRIGHT_BROWSER_MODE === "bundled" && !getBundledPlaywrightBrowserDir()) {
    problems.push(`missing bundled Playwright Chromium under ${PLAYWRIGHT_BROWSERS_DIR}`);
  }

  if (app.isPackaged && PLAYWRIGHT_BROWSER_MODE === "system" && !getSystemPlaywrightBrowser()) {
    problems.push("no supported local Microsoft Edge or Google Chrome installation found for thin-no-browser mode");
  }

  const requiredCommands = [];
  if (!app.isPackaged && !USE_STATIC_FRONTEND) {
    requiredCommands.push("pnpm");
  }
  if (!app.isPackaged && !USE_STATIC_FRONTEND && IS_WINDOWS) {
    requiredCommands.push("git");
  }
  for (const cmd of requiredCommands) {
    if (!commandExists(cmd)) {
      problems.push(`command not found on PATH: ${cmd}`);
    }
  }

  if (!fs.existsSync(RUNTIME_CONFIG_PATH) && !fs.existsSync(FALLBACK_CONFIG_PATH)) {
    problems.push(`missing config: neither ${RUNTIME_CONFIG_PATH} nor ${FALLBACK_CONFIG_PATH} exists`);
  }

  return problems;
};

const spawnLogged = (command, args, cwd, logName, envOverrides = {}) => {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const extensionsConfigPath = ensureDesktopExtensionsConfig();
  const packagedSkillsPath = path.join(process.resourcesPath, "runtime", "skills");
  const systemPlaywrightBrowser = getSystemPlaywrightBrowser();
  const playwrightEnv = {
    DEER_FLOW_PLAYWRIGHT_BROWSER_MODE: PLAYWRIGHT_BROWSER_MODE
  };
  if (process.env.PLAYWRIGHT_BROWSERS_PATH) {
    playwrightEnv.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH;
  } else if (PLAYWRIGHT_BROWSER_MODE === "bundled" && app.isPackaged) {
    playwrightEnv.PLAYWRIGHT_BROWSERS_PATH = PLAYWRIGHT_BROWSERS_DIR;
  }
  if (systemPlaywrightBrowser && systemPlaywrightBrowser.channel) {
    playwrightEnv.DEER_FLOW_PLAYWRIGHT_BROWSER_CHANNEL = systemPlaywrightBrowser.channel;
  }
  if (systemPlaywrightBrowser && systemPlaywrightBrowser.executablePath) {
    playwrightEnv.DEER_FLOW_PLAYWRIGHT_EXECUTABLE_PATH = systemPlaywrightBrowser.executablePath;
  }
  const out = fs.openSync(path.join(LOG_DIR, logName), "a");
  const child = spawn(command, args, {
    cwd,
    env: {
      ...process.env,
      DEEPSEEK_LOCAL_MODEL: process.env.DEEPSEEK_LOCAL_MODEL || "DeepSeekV4",
      DEEPSEEK_LOCAL_INTERFACE_MODE: process.env.DEEPSEEK_LOCAL_INTERFACE_MODE || "both",
      DEER_FLOW_SANDBOX_HOST_ROOT: process.env.DEER_FLOW_SANDBOX_HOST_ROOT || USER_HOME || "",
      DEER_FLOW_SANDBOX_PROJECT_ROOT:
        process.env.DEER_FLOW_SANDBOX_PROJECT_ROOT || path.join(USER_HOME || "", "Downloads"),
      DEER_FLOW_HOME: process.env.DEER_FLOW_HOME || PORTABLE_DATA_DIR,
      DEER_FLOW_EXTENSIONS_CONFIG_PATH:
        process.env.DEER_FLOW_EXTENSIONS_CONFIG_PATH ||
        extensionsConfigPath,
      DEER_FLOW_SKILLS_PATH:
        process.env.DEER_FLOW_SKILLS_PATH ||
        (app.isPackaged ? packagedSkillsPath : undefined),
      DEER_FLOW_HOST_SKILLS_PATH:
        process.env.DEER_FLOW_HOST_SKILLS_PATH ||
        (app.isPackaged ? packagedSkillsPath : undefined),
      ...playwrightEnv,
      ...envOverrides
    },
    stdio: ["ignore", out, out]
  });
  child.on("error", () => {});
  return child;
};

const waitUntilReady = async (timeoutMs = 30000) => {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const [providerRes, gatewayRes, webRes] = await Promise.all([
        fetch(`http://${PROVIDER_HOST}:${PROVIDER_PORT}/health`),
        fetch(`http://127.0.0.1:${GATEWAY_PORT}/api/models`),
        fetch(`http://127.0.0.1:${WEB_PORT}`)
      ]);
      if (providerRes.ok && gatewayRes.ok && webRes.ok) {
        return true;
      }
    } catch {
      // ignore
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
};

const waitProviderReady = async (timeoutMs = 12000) => {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const providerRes = await fetch(`http://${PROVIDER_HOST}:${PROVIDER_PORT}/health`);
      if (providerRes.ok) {
        return true;
      }
    } catch {
      // ignore
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
};

const openProviderLogin = async (provider = "deepseek") => {
  const ok = await waitProviderReady(12000);
  if (!ok) {
    await dialog.showMessageBox({
      type: "warning",
      title: "Provider Not Ready",
      message: `${provider === "xiaomi" ? "Xiaomi MiMo" : "DeepSeek"} provider is still starting. Please try login again in a few seconds.`
    });
    return;
  }

  try {
    const model = PROVIDER_LOGIN_MODELS[provider] || DEFAULT_MODEL;
    const res = await fetch(
      `http://${PROVIDER_HOST}:${PROVIDER_PORT}/debug/open-login?model=${encodeURIComponent(model)}`,
      { method: "POST" }
    );
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    logLine(`open-login triggered provider=${provider} model=${model}`);
  } catch (error) {
    logLine(`open-login failed: ${String(error)}`);
    await dialog.showMessageBox({
      type: "error",
      title: "Open Login Failed",
      message: `Failed to open ${provider === "xiaomi" ? "Xiaomi MiMo" : "DeepSeek"} login window.`,
      detail: String(error)
    });
  }
};

const stopAll = async (timeoutMs = 8000) => {
  logLine("stopAll invoked");
  killTrackedChildren();

  if (IS_WINDOWS) {
    // stop-mac-desktop.sh is mac-specific; on Windows tracked child termination is best-effort.
    await new Promise((resolve) => setTimeout(resolve, 200));
    return;
  }

  await Promise.race([
    new Promise((resolve) => {
      const stopper = spawn("bash", ["-lc", "./scripts/stop-mac-desktop.sh"], {
        cwd: REPO_ROOT,
        stdio: "ignore"
      });
      stopper.on("exit", () => resolve());
      stopper.on("error", () => resolve());
    }),
    new Promise((resolve) =>
      setTimeout(() => {
        logLine("stopAll timeout, continue quitting");
        resolve();
      }, timeoutMs)
    )
  ]);
};

const killTrackedChildren = () => {
  if (serveProc && !serveProc.killed) {
    try {
      serveProc.kill("SIGTERM");
    } catch {}
  }
  if (frontendProc && !frontendProc.killed) {
    try {
      frontendProc.kill("SIGTERM");
    } catch {}
  }
  if (gatewayProc && !gatewayProc.killed) {
    try {
      gatewayProc.kill("SIGTERM");
    } catch {}
  }
  if (providerProc && !providerProc.killed) {
    try {
      providerProc.kill("SIGTERM");
    } catch {}
  }
  serveProc = null;
  frontendProc = null;
  gatewayProc = null;
  providerProc = null;
};

const startAttempt = async (serveCommand, serveArgs, label, envOverrides = {}, timeoutMs = 35000) => {
  logLine(`start attempt: ${label}`);
  const pythonRuntime = resolvePythonRuntime();
  if (!pythonRuntime) {
    throw new Error("No Python runtime available for provider startup.");
  }
  providerProc = spawnLogged(
    pythonRuntime.command,
    pythonModuleArgs(pythonRuntime, "uvicorn", [
      "app.deepseek_local_provider:app",
      "--host",
      PROVIDER_HOST,
      "--port",
      String(PROVIDER_PORT)
    ]),
    BACKEND_DIR,
    "electron_provider.log"
  );

  serveProc = spawnLogged(serveCommand, serveArgs, REPO_ROOT, "electron_serve.log", envOverrides);

  const ready = await waitUntilReady(timeoutMs);
  if (!ready) {
    logLine(`start attempt timeout: ${label}`);
    killTrackedChildren();
    await stopAll();
    throw new Error(`Desktop profile '${label}' failed to start in time.`);
  }
  logLine(`start attempt success: ${label}`);
};

const spawnStaticFrontendServer = () => {
  const frontendEnv = {
    ELECTRON_RUN_AS_NODE: "1",
    HOSTNAME: "127.0.0.1",
    PORT: String(WEB_PORT),
    DEER_FLOW_STATIC_ROOT: FRONTEND_STATIC_DIR,
  };
  return spawnLogged(
    process.execPath,
    [STATIC_SERVER_ENTRY],
    REPO_ROOT,
    "electron_frontend.log",
    frontendEnv
  );
};

const startStaticDesktopServices = async () => {
  const configPath = fs.existsSync(RUNTIME_CONFIG_PATH) ? RUNTIME_CONFIG_PATH : FALLBACK_CONFIG_PATH;
  const pythonRuntime = resolvePythonRuntime();
  if (!pythonRuntime) {
    throw new Error("No Python runtime available for desktop services.");
  }
  const extensionsConfigPath = ensureDesktopExtensionsConfig();
  logLine(`start static desktop services config=${configPath}`);
  logLine(`extensions config=${extensionsConfigPath}`);
  logLine(`python runtime=${pythonRuntime.kind}`);

  providerProc = spawnLogged(
    pythonRuntime.command,
    pythonModuleArgs(pythonRuntime, "uvicorn", [
      "app.deepseek_local_provider:app",
      "--host",
      PROVIDER_HOST,
      "--port",
      String(PROVIDER_PORT)
    ]),
    BACKEND_DIR,
    "electron_provider.log",
    { DEER_FLOW_CONFIG_PATH: configPath }
  );

  gatewayProc = spawnLogged(
    pythonRuntime.command,
    pythonModuleArgs(pythonRuntime, "uvicorn", [
      "app.gateway.app:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(GATEWAY_PORT)
    ]),
    BACKEND_DIR,
    "electron_gateway.log",
    {
      PYTHONPATH: ".",
      SKIP_LANGGRAPH_SERVER: "1",
      DEER_FLOW_CONFIG_PATH: configPath,
    }
  );

  frontendProc = spawnStaticFrontendServer();

  const ready = await waitUntilReady(PACKAGED_INSTALL_TIMEOUT_MS);
  if (!ready) {
    killTrackedChildren();
    throw new Error("Static desktop services failed to start in time.");
  }
  logLine("start static desktop services success");
};

const startServices = async () => {
  const startupProblems = collectStartupProblems();
  if (startupProblems.length > 0) {
    throw new Error(
      `Startup preflight failed:\n- ${startupProblems.join("\n- ")}\n\n` +
        "This desktop package currently depends on DeerFlow runtime files and local developer tools."
    );
  }

  const alreadyReady = await waitUntilReady(1200);
  if (alreadyReady) {
    logLine("reuse existing services");
    return;
  }

  await stopAll();
  if (USE_STATIC_FRONTEND) {
    await startStaticDesktopServices();
    return;
  }
  const makeServeLaunch = (serveCmd) => {
    if (IS_WINDOWS) {
      return {
        command: "cmd",
        args: ["/c", "scripts\\run-with-git-bash.cmd", "-lc", serveCmd]
      };
    }
    return {
      command: "bash",
      args: ["-lc", serveCmd]
    };
  };

  const attempts =
    DESKTOP_PROFILE === "low-memory"
      ? [
          {
            label: "prod-gateway",
            ...makeServeLaunch(`./scripts/serve.sh --prod --gateway --daemon${SHOULD_SKIP_INSTALL ? " --skip-install" : ""}`),
            env: { DEER_FLOW_IGNORE_BUILD_ERRORS: "1" },
            timeoutMs: SHOULD_SKIP_INSTALL ? (IS_WINDOWS ? 240000 : 180000) : PACKAGED_INSTALL_TIMEOUT_MS
          },
          {
            label: "dev-gateway-fallback",
            ...makeServeLaunch(`./scripts/serve.sh --dev --gateway --daemon${SHOULD_SKIP_INSTALL ? " --skip-install" : ""}`),
            env: {},
            timeoutMs: SHOULD_SKIP_INSTALL ? (IS_WINDOWS ? 120000 : 35000) : PACKAGED_INSTALL_TIMEOUT_MS
          }
        ]
      : [
          {
            label: "dev-gateway",
            ...makeServeLaunch(`./scripts/serve.sh --dev --gateway --daemon${SHOULD_SKIP_INSTALL ? " --skip-install" : ""}`),
            env: {},
            timeoutMs: SHOULD_SKIP_INSTALL ? (IS_WINDOWS ? 120000 : 35000) : PACKAGED_INSTALL_TIMEOUT_MS
          }
        ];

  let lastError = null;
  for (const attempt of attempts) {
    try {
      await startAttempt(attempt.command, attempt.args, attempt.label, attempt.env, attempt.timeoutMs);
      return;
    } catch (error) {
      lastError = error;
    }
  }

  throw lastError || new Error("DeerFlow services were not ready in time.");
};

const createWindow = () => {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 1080,
    minHeight: 700,
    title: "DeerFlowWithDeepSeek",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      sandbox: true
    }
  });

  const bootHtml = `
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DeerFlowWithDeepSeek</title>
  <style>
    :root { color-scheme: light; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: grid;
      place-items: center;
      height: 100vh;
      background: #f6f1e7;
      color: #201c16;
    }
    .card {
      width: min(420px, 88vw);
      background: rgba(255,255,255,0.92);
      border: 1px solid #e6ddd0;
      border-radius: 18px;
      padding: 28px 28px 24px;
      box-shadow: 0 16px 48px rgba(58, 42, 17, 0.08);
    }
    .spinner {
      width: 24px;
      height: 24px;
      border-radius: 999px;
      border: 2px solid rgba(32, 28, 22, 0.14);
      border-top-color: #201c16;
      animation: spin 0.8s linear infinite;
    }
    .title {
      margin-top: 18px;
      font-size: 24px;
      font-weight: 650;
      letter-spacing: -0.02em;
    }
    .desc {
      margin-top: 10px;
      font-size: 14px;
      line-height: 1.7;
      color: rgba(32, 28, 22, 0.68);
    }
    .row {
      margin-top: 18px;
      font-size: 13px;
      color: rgba(32, 28, 22, 0.62);
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="spinner"></div>
    <div class="title">正在启动 DeerFlow</div>
    <div class="desc">稍后会进入简洁首页，那里会显示账号就绪状态，并提供进入工作区按钮。</div>
    <div class="row">正在连接：Provider / Gateway / Frontend ...</div>
  </div>
</body>
</html>
  `;
  mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(bootHtml)}`);
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith("deerflow://")) {
      return;
    }
    event.preventDefault();
    if (url.startsWith("deerflow://open-login")) {
      const parsed = new URL(url);
      const provider = parsed.searchParams.get("provider") || "deepseek";
      void openProviderLogin(provider);
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
    if (!quitting) {
      app.quit();
    }
  });
};

const installAppMenu = () => {
  const template = [
    {
      label: "DeerFlowWithDeepSeek",
      submenu: [
        {
          label: "打开 DeepSeek 登录",
          click: () => {
            void openProviderLogin("deepseek");
          }
        },
        {
          label: "打开 Xiaomi MiMo 登录",
          click: () => {
            void openProviderLogin("xiaomi");
          }
        },
        { type: "separator" },
        { role: "quit", label: "退出" }
      ]
    },
    { role: "editMenu" },
    { role: "viewMenu" },
    { role: "windowMenu" }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
};

app.whenReady().then(async () => {
  installAppMenu();
  createWindow();
  logLine("window created");

  try {
    logLine(`app ready; desktop profile=${DESKTOP_PROFILE}`);
    await startServices();
    if (mainWindow && !mainWindow.isDestroyed() && !quitting) {
      await mainWindow.loadURL(APP_URL);
      logLine("workspace loaded");
    }
  } catch (error) {
    logLine(`startup failed: ${String(error)}`);
    await dialog.showMessageBox({
      type: "error",
      title: "Startup Failed",
      message: "Failed to start DeerFlowWithDeepSeek desktop services.",
      detail: String(error)
    });
    app.quit();
  }
});

app.on("second-instance", () => {
  if (!mainWindow) {
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.focus();
});

app.on("before-quit", async (event) => {
  if (quitting) {
    return;
  }
  event.preventDefault();
  quitting = true;
  logLine("before-quit");
  await stopAll();
  app.quit();
});

app.on("window-all-closed", () => {
  if (!quitting) {
    app.quit();
  }
});
