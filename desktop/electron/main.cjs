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
    if (fs.existsSync(path.join(candidate, "backend")) && fs.existsSync(path.join(candidate, "scripts"))) {
      return candidate;
    }
  }
  return packagedCandidates[0];
};

const REPO_ROOT = resolveRepoRoot();
const BACKEND_DIR = path.join(REPO_ROOT, "backend");
const LOG_DIR = app.isPackaged
  ? path.join(USER_HOME || process.cwd(), ".deerflowwithdeepseek", "logs")
  : path.join(REPO_ROOT, "logs");

const PROVIDER_HOST = process.env.DEEPSEEK_LOCAL_PROVIDER_HOST || "127.0.0.1";
const PROVIDER_PORT = Number(process.env.DEEPSEEK_LOCAL_PROVIDER_PORT || "8765");
const WEB_PORT = Number(process.env.DEER_FLOW_WEB_PORT || "2026");
const DESKTOP_PROFILE = (process.env.DEER_FLOW_DESKTOP_PROFILE || "dev").toLowerCase();
const DEFAULT_MODEL = process.env.DEEPSEEK_LOCAL_MODEL || "DeepSeekV4";
const SHOULD_SKIP_INSTALL = !app.isPackaged;
const PACKAGED_INSTALL_TIMEOUT_MS = IS_WINDOWS ? 600000 : 240000;

let mainWindow = null;
let providerProc = null;
let serveProc = null;
let quitting = false;
const WORKSPACE_URL = `http://127.0.0.1:${WEB_PORT}/workspace`;

const logLine = (msg) => {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(path.join(LOG_DIR, "electron_desktop.log"), `[${new Date().toISOString()}] ${msg}\n`);
  } catch {
    // no-op
  }
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

const collectStartupProblems = () => {
  const problems = [];
  const requiredPaths = [
    ["repo root", REPO_ROOT],
    ["backend dir", BACKEND_DIR],
    ["scripts dir", path.join(REPO_ROOT, "scripts")],
    ["serve.sh", path.join(REPO_ROOT, "scripts", "serve.sh")]
  ];
  if (IS_WINDOWS) {
    requiredPaths.push(["run-with-git-bash.cmd", path.join(REPO_ROOT, "scripts", "run-with-git-bash.cmd")]);
  }
  for (const [name, p] of requiredPaths) {
    if (!fs.existsSync(p)) {
      problems.push(`missing ${name}: ${p}`);
    }
  }

  const requiredCommands = ["uv", "pnpm"];
  if (IS_WINDOWS) {
    requiredCommands.push("git");
  }
  for (const cmd of requiredCommands) {
    if (!commandExists(cmd)) {
      problems.push(`command not found on PATH: ${cmd}`);
    }
  }

  return problems;
};

const spawnLogged = (command, args, cwd, logName, envOverrides = {}) => {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  const out = fs.openSync(path.join(LOG_DIR, logName), "a");
  const child = spawn(command, args, {
    cwd,
    env: {
      ...process.env,
      DEEPSEEK_LOCAL_MODEL: process.env.DEEPSEEK_LOCAL_MODEL || "DeepSeekV4",
      DEEPSEEK_LOCAL_INTERFACE_MODE: process.env.DEEPSEEK_LOCAL_INTERFACE_MODE || "both",
      DEER_FLOW_SANDBOX_HOST_ROOT: process.env.DEER_FLOW_SANDBOX_HOST_ROOT || process.env.HOME || "",
      DEER_FLOW_SANDBOX_PROJECT_ROOT:
        process.env.DEER_FLOW_SANDBOX_PROJECT_ROOT || path.join(process.env.HOME || "", "Downloads"),
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
      const [providerRes, webRes] = await Promise.all([
        fetch(`http://${PROVIDER_HOST}:${PROVIDER_PORT}/health`),
        fetch(`http://127.0.0.1:${WEB_PORT}`)
      ]);
      if (providerRes.ok && webRes.ok) {
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

const openProviderLogin = async () => {
  const ok = await waitProviderReady(12000);
  if (!ok) {
    await dialog.showMessageBox({
      type: "warning",
      title: "Provider Not Ready",
      message: "DeepSeek provider is still starting. Please try login again in a few seconds."
    });
    return;
  }

  try {
    const res = await fetch(
      `http://${PROVIDER_HOST}:${PROVIDER_PORT}/debug/open-login?model=${encodeURIComponent(DEFAULT_MODEL)}`,
      { method: "POST" }
    );
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    logLine("open-login triggered");
  } catch (error) {
    logLine(`open-login failed: ${String(error)}`);
    await dialog.showMessageBox({
      type: "error",
      title: "Open Login Failed",
      message: "Failed to open DeepSeek login window.",
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
  if (providerProc && !providerProc.killed) {
    try {
      providerProc.kill("SIGTERM");
    } catch {}
  }
  serveProc = null;
  providerProc = null;
};

const startAttempt = async (serveCommand, serveArgs, label, envOverrides = {}, timeoutMs = 35000) => {
  logLine(`start attempt: ${label}`);
  providerProc = spawnLogged(
    "uv",
    ["run", "uvicorn", "app.deepseek_local_provider:app", "--host", PROVIDER_HOST, "--port", String(PROVIDER_PORT)],
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
    :root { color-scheme: light dark; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: grid;
      place-items: center;
      height: 100vh;
      background: linear-gradient(145deg, #f6f8fb, #edf2f7);
      color: #111827;
    }
    .card {
      width: min(560px, 90vw);
      background: rgba(255,255,255,0.88);
      border: 1px solid rgba(15,23,42,0.08);
      border-radius: 14px;
      padding: 24px 26px;
      box-shadow: 0 18px 50px rgba(2, 6, 23, 0.14);
    }
    .title { font-size: 20px; font-weight: 650; margin-bottom: 8px; }
    .desc { opacity: .76; font-size: 14px; line-height: 1.55; }
    .row { margin-top: 14px; font-size: 13px; opacity: .78; }
    .actions { margin-top: 16px; display: flex; gap: 10px; }
    .btn {
      border: 1px solid #d1d5db;
      background: white;
      color: #111827;
      padding: 8px 12px;
      border-radius: 10px;
      font-size: 13px;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
    }
    .btn.primary {
      background: #2563eb;
      color: white;
      border-color: #2563eb;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="title">DeerFlowWithDeepSeek 正在启动</div>
    <div class="desc">窗口已打开，后端服务会在后台继续启动。启动完成后会自动进入工作区。</div>
    <div class="row">正在连接：Provider / Gateway / Frontend ...</div>
    <div class="actions">
      <a class="btn primary" href="deerflow://open-login">登录 DeepSeek</a>
    </div>
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
    if (url === "deerflow://open-login") {
      void openProviderLogin();
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
            void openProviderLogin();
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
      await mainWindow.loadURL(WORKSPACE_URL);
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
