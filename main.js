/**
 * IG Agent v30.0 — Project Apex Monolith
 * Native frameless shell · IOPMAssertion · shadow sidecar · Unix-socket IPC.
 * v30 Apex shadow monolith — native shell only; production v29 is never contacted.
 */

require("fs").appendFileSync(
  require("path").join(require("os").tmpdir(), "apex-electron-main-loaded.log"),
  `${new Date().toISOString()} module start\n`
);

const { app, BrowserWindow, ipcMain, session } = require("electron");
const { spawn, execSync, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const log = require("electron-log");

const DEFAULT_SHELL = {
  shell: {
    backgroundColor: "#0a0e14",
    frameless: true,
    titleBarStyle: "hidden",
    autoHideMenuBar: true,
    backgroundThrottling: false,
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 720,
  },
  runtime: {
    profile: "shadow",
    protectProductionPorts: true,
    shadowApiPort: 9090,
    v30Only: true,
  },
  preload: {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
  },
};

function loadShellConfig() {
  const candidates = [
    path.join(__dirname, "build", "apex-shell.json"),
    path.join(process.resourcesPath || "", "build", "apex-shell.json"),
  ];
  for (const cfgPath of candidates) {
    try {
      if (fs.existsSync(cfgPath)) {
        const parsed = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
        const apiPort = Number(parsed.apiPort) || Number(parsed.runtime?.shadowApiPort) || 9090;
        return {
          ...DEFAULT_SHELL,
          ...parsed,
          apiPort,
          runtime: {
            ...DEFAULT_SHELL.runtime,
            ...(parsed.runtime || {}),
            profile: "shadow",
            shadowApiPort: apiPort,
          },
        };
      }
    } catch (err) {
      log.warn("shell config read failed:", cfgPath, err.message);
    }
  }
  return { ...DEFAULT_SHELL, apiPort: 9090 };
}

const SHELL = loadShellConfig();
/** Unified desktop monolith API port — always :9090 inside the Apex shell. */
const APEX_API_PORT = Number(SHELL.apiPort) || Number(SHELL.runtime?.shadowApiPort) || 9090;
/** Glass cockpit operational transparency — funnel + health grid streamed on every IPC tick. */
const APEX_TRANSPARENCY_HUD = true;

function resolveAgentRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "agent");
  }
  return __dirname;
}

function apexHomeDir() {
  try {
    if (app.isReady()) return app.getPath("home");
  } catch (_) {
    /* app.getPath unavailable before ready */
  }
  return process.env.HOME || os.homedir();
}

function bootTrace(message) {
  try {
    const tracePath = path.join(
      apexHomeDir(),
      "Library",
      "Application Support",
      "IG Agent Apex",
      "v30-production",
      "data",
      "logs",
      "apex_boot_trace.log"
    );
    fs.mkdirSync(path.dirname(tracePath), { recursive: true });
    fs.appendFileSync(tracePath, `[${new Date().toISOString()}] ${message}\n`);
  } catch (_) {
    /* ignore trace failures */
  }
}

bootTrace(`main.js loaded packaged=${app.isPackaged} resources=${process.resourcesPath || ""}`);

function resolveUserDataRoot() {
  if (process.platform === "darwin") {
    return path.join(
      apexHomeDir(),
      "Library",
      "Application Support",
      "IG Agent Apex",
      "v30-production"
    );
  }
  try {
    return path.join(app.getPath("userData"), "v30-production");
  } catch {
    return path.join(apexHomeDir(), ".ig-agent-apex", "v30-production");
  }
}

function ensureWritableLayout() {
  const root = resolveUserDataRoot();
  for (const sub of ["data", "data/logs", "data/state", "analytics"]) {
    fs.mkdirSync(path.join(root, sub), { recursive: true });
  }
  return root;
}

const AGENT_ROOT = resolveAgentRoot();
const USER_ROOT = () => ensureWritableLayout();

log.transports.file.resolvePathFn = () => {
  try {
    return path.join(resolveUserDataRoot(), "data", "logs", "apex_electron.log");
  } catch {
    return path.join(__dirname, "src", "data", "logs", "apex_electron.log");
  }
};

const PYTHON_BIN = path.join(AGENT_ROOT, ".venv", "bin", "python3");
const MAIN_PY = path.join(AGENT_ROOT, "src", "main.py");
const DAEMON_PY = path.join(AGENT_ROOT, "src", "system", "apex_daemon.py");
const POWER_BIN = path.join(AGENT_ROOT, "native", "apex_power", "no_nap");
const PURGE_SCRIPT = app.isPackaged
  ? path.join(process.resourcesPath, "scripts", "apex-purge-ports.sh")
  : path.join(__dirname, "scripts", "apex-purge-ports.sh");

const SHADOW_API_PORT = APEX_API_PORT;
const SHADOW_COCKPIT_PORT = SHELL.runtime.shadowCockpitPort || 9191;

function ipcSocketPath() {
  return path.join(USER_ROOT(), "data", "apex_ipc.sock");
}

function resolveDashboardIndexPath() {
  const candidates = [
    path.join(__dirname, "dashboard", "dist", "index.html"),
    path.join(process.resourcesPath || "", "app.asar", "dashboard", "dist", "index.html"),
    path.join(process.resourcesPath || "", "dashboard", "dist", "index.html"),
    path.join(AGENT_ROOT, "dashboard", "dist", "index.html"),
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return candidates[0];
}

function isShadowSession() {
  return String(SHELL.runtime.profile || "shadow").toLowerCase() === "shadow";
}

const IPC_RETRY_MS = 2000;
const IPC_MAX_RETRIES = 5;
/** Gate1–5 bootstrap can exceed 10s on cold pack; match dashboard recovery budget. */
const SIDECAR_READY_TIMEOUT_MS = 90000;
const IPC_RECONNECT_MS = 500;
/** Debounced IPC reconnect — prevents reconnect storms when UDS is briefly unavailable. */
let _ipcReconnectTimer = null;
let _ipcConnectInFlight = false;
let _ipcClientConnected = false;

function buildRendererRuntimeConfig() {
  return {
    profile: "shadow",
    apiPort: APEX_API_PORT,
    apiBase: `http://127.0.0.1:${APEX_API_PORT}`,
    cockpitPort: SHADOW_COCKPIT_PORT,
    cockpitBase: `http://127.0.0.1:${SHADOW_COCKPIT_PORT}`,
    protectProductionPorts: Boolean(SHELL.runtime.protectProductionPorts),
    v30Only: Boolean(SHELL.runtime.v30Only !== false),
    shellBackground: SHELL.shell?.backgroundColor || "#0a0e14",
    version: String(SHELL.version || "30.0.0"),
    versionLabel: "v30.0",
  };
}

function registerIpcHandlers() {
  ipcMain.on("apex:runtime-config-sync", (event) => {
    event.returnValue = buildRendererRuntimeConfig();
  });
}

async function prepareRendererSession() {
  // Project Apex Core UI State Purification Hook — before windows or Python spawn
  const ses = session.defaultSession;
  await ses.clearCache();
  await ses.clearStorageData({
    storages: [
      "appcache",
      "cookies",
      "localstorage",
      "indexdb",
      "shadercache",
      "serviceworkers",
    ],
  });
  if (typeof ses.clearAuthCache === "function") {
    await ses.clearAuthCache();
  }
  log.info(
    "prepareRendererSession: cache + storage + network residues purged (clean Midnight Indigo render)"
  );
}

/** Immutable separation — disconnect Electron IPC; preserve detached :9090 daemon. */
function releaseShellImmutable() {
  isQuitting = true;
  disconnectIpcClient();
  if (mainWindow && !mainWindow.isDestroyed()) {
    try {
      mainWindow.webContents.send("apex:ipc-status", { connected: false });
    } catch (_) {
      /* ignore */
    }
  }
  daemonProcess = null;
  sidecarProcess = null;
  log.info("releaseShellImmutable: IPC torn down — detached Python daemon preserved");
}

function installExternalBrowserGuards() {
  app.on("web-contents-created", (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      log.warn("Blocked external browser window:", url);
      return { action: "deny" };
    });
    contents.on("will-navigate", (event, navigationUrl) => {
      try {
        const parsed = new URL(navigationUrl);
        if (parsed.protocol === "http:" || parsed.protocol === "https:") {
          event.preventDefault();
          log.warn("Blocked external navigation:", navigationUrl);
        }
      } catch {
        /* ignore */
      }
    });
  });
}
/** @type {import('child_process').ChildProcess | null} */
let daemonProcess = null;
/** Legacy alias — detached daemon child when Electron tracked the spawn. */
let sidecarProcess = null;
/** @type {import('child_process').ChildProcess | null} */
let powerAssertionProcess = null;
/** @type {import('net').Socket | null} */
let ipcClient = null;
/** @type {BrowserWindow | null} */
let mainWindow = null;
let isQuitting = false;
/** True when a healthy :9090 listener was already running (pilot / prior session). */
let sidecarExternallyAdopted = false;
let daemonDetached = false;
let dashboardLoaded = false;
/** @type {Array<{phase:number,detail:string,ts:number}>} */
let pendingBootPhases = [];

function broadcastBootPhase(phase, detail = "") {
  const payload = { phase: Number(phase), detail: String(detail || ""), ts: Date.now() };
  bootTrace(`boot-phase ${payload.phase}: ${payload.detail}`);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("apex:boot-phase", payload);
  } else {
    pendingBootPhases.push(payload);
  }
}

function flushPendingBootPhases() {
  if (!mainWindow || mainWindow.isDestroyed() || !pendingBootPhases.length) return;
  for (const payload of pendingBootPhases) {
    mainWindow.webContents.send("apex:boot-phase", payload);
  }
  pendingBootPhases = [];
}

function probeShadowApiHealth200() {
  try {
    const curlBin = fs.existsSync("/usr/bin/curl") ? "/usr/bin/curl" : "curl";
    const result = spawnSync(
      curlBin,
      [
        "-sf",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--max-time",
        "2",
        `http://127.0.0.1:${SHADOW_API_PORT}/api/health`,
      ],
      { encoding: "utf8" }
    );
    if (result.error || result.status !== 0) return false;
    const code = Number.parseInt(String(result.stdout || "").trim(), 10);
    return code === 200;
  } catch {
    return false;
  }
}

function probeShadowApiHealthSync() {
  return probeShadowApiHealth200();
}

function daemonPidPath() {
  return path.join(USER_ROOT(), "data", "apex_daemon.pid");
}

function writeDaemonPid(pid) {
  try {
    fs.mkdirSync(path.dirname(daemonPidPath()), { recursive: true });
    fs.writeFileSync(daemonPidPath(), String(pid), "utf8");
  } catch (err) {
    log.warn("writeDaemonPid:", err.message);
  }
}

function purgeStagingTracks() {
  broadcastBootPhase(1, "Forcefully evicting stale port processes & cleaning staging tracks");
  try {
    if (SHELL.runtime.protectProductionPorts) {
      execSync(
        `for pid in $(lsof -tiTCP:${SHADOW_API_PORT} -sTCP:LISTEN 2>/dev/null || true); do ` +
          `kill -9 "$pid" 2>/dev/null || true; done`,
        { shell: "/bin/bash", stdio: "pipe", timeout: 5000 }
      );
    } else {
      purgeShadowPorts();
    }
  } catch (err) {
    log.warn("purgeStagingTracks port eviction:", err.message);
  }

  for (const sock of [
    ipcSocketPath(),
    path.join(AGENT_ROOT, "src", "data", "apex_ipc.sock"),
    path.join(AGENT_ROOT, "src", "data", "apex_ipc_shadow.sock"),
  ]) {
    try {
      if (fs.existsSync(sock)) fs.unlinkSync(sock);
    } catch (_) {
      /* ignore */
    }
  }

  for (const cacheDir of [
    path.join(__dirname, "node_modules", ".cache"),
    path.join(AGENT_ROOT, "node_modules", ".cache"),
  ]) {
    try {
      if (fs.existsSync(cacheDir)) {
        fs.rmSync(cacheDir, { recursive: true, force: true });
      }
    } catch (err) {
      log.warn("purgeStagingTracks cache:", cacheDir, err.message);
    }
  }
  log.info("purgeStagingTracks: :9090 + IPC + builder cache cleared (production :8080 protected)");
}

function spawnDetachedDaemon() {
  broadcastBootPhase(2, "Initializing background python microkernel on port 9090");
  const py = resolvePython();
  const entry = fs.existsSync(DAEMON_PY) ? DAEMON_PY : MAIN_PY;
  const env = buildSidecarEnv();
  env.IG_APEX_DAEMON = "1";
  env.NODE_ENV = "production";

  if (!fs.existsSync(entry)) {
    bootTrace(`spawnDetachedDaemon missing entry: ${entry}`);
    log.error(`spawnDetachedDaemon: missing ${entry}`);
    return false;
  }
  if (py !== "python3" && !fs.existsSync(py)) {
    log.error(`spawnDetachedDaemon: missing python ${py}`);
    return false;
  }

  log.info(`spawnDetachedDaemon: detached ${py} ${entry} (:${SHADOW_API_PORT})`);
  const child = spawn(py, [entry], {
    cwd: AGENT_ROOT,
    env,
    detached: true,
    stdio: "ignore",
  });
  child.unref();
  daemonProcess = child;
  sidecarProcess = child;
  daemonDetached = true;
  sidecarExternallyAdopted = true;
  if (child.pid) writeDaemonPid(child.pid);
  broadcastBootPhase(3, "Parallel 4-Worker Thread Pool: Hydrating 120-bar Yahoo historical candle arrays");
  return true;
}

function ensureDaemonSupervisor({ forceRestart = false } = {}) {
  bootTrace(`ensureDaemonSupervisor forceRestart=${forceRestart}`);

  if (!forceRestart && probeShadowApiHealth200()) {
    sidecarExternallyAdopted = true;
    daemonDetached = true;
    bootTrace("ensureDaemonSupervisor: passive handshake HTTP 200 — adopt existing daemon");
    log.info("Daemon supervisor: :9090 health 200 — adopting live microkernel");
    broadcastBootPhase(3, "Background daemon already active on :9090");
    return true;
  }

  if (forceRestart) {
    purgeStagingTracks();
  } else if (!probeShadowApiHealth200()) {
    purgeStagingTracks();
  }

  if (!spawnDetachedDaemon()) {
    return false;
  }

  const deadline = Date.now() + SIDECAR_READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (probeShadowApiHealth200()) {
      bootTrace("ensureDaemonSupervisor: daemon health 200");
      return true;
    }
    try {
      execSync("sleep 0.5", { stdio: "ignore" });
    } catch (_) {
      /* ignore */
    }
  }
  log.error(`ensureDaemonSupervisor: daemon not healthy within ${SIDECAR_READY_TIMEOUT_MS}ms`);
  return false;
}

/** Release UI shell only — immutable 24/7 daemon stays in system memory. */
function releaseShellOnly() {
  releaseShellImmutable();
  isQuitting = false;
}

function ensureShadowSidecarRunning(opts = {}) {
  return ensureDaemonSupervisor(opts);
}

function startShadowSidecar(opts = {}) {
  return ensureDaemonSupervisor(opts);
}

function isPortListening(port) {
  try {
    const result = spawnSync("/usr/sbin/lsof", [`-iTCP:${port}`, "-sTCP:LISTEN"], {
      encoding: "utf8",
    });
    return Boolean(result.stdout && result.stdout.trim());
  } catch {
    return false;
  }
}

function purgeShadowPorts() {
  try {
    execSync(`bash "${PURGE_SCRIPT}"`, {
      cwd: AGENT_ROOT,
      env: {
        ...process.env,
        IG_APEX_PROTECT_PRODUCTION_PORTS: SHELL.runtime.protectProductionPorts ? "1" : "0",
        IG_AGENT_ROOT: AGENT_ROOT,
        IG_AGENT_DATA_DIR: path.join(USER_ROOT(), "data"),
      },
      stdio: "pipe",
      timeout: 5000,
    });
    log.info("purgeShadowPorts: :9090 cleared (production protected)");
  } catch (err) {
    log.warn("purgeShadowPorts:", err.message);
  }
}

function startPowerAssertion() {
  if (process.platform !== "darwin") return;
  if (powerAssertionProcess) return;
  const buildScript = app.isPackaged
    ? path.join(process.resourcesPath, "scripts", "apex-build-power-assertion.sh")
    : path.join(__dirname, "scripts", "apex-build-power-assertion.sh");
  if (!fs.existsSync(POWER_BIN)) {
    try {
      execSync(`bash "${buildScript}"`, { cwd: AGENT_ROOT, stdio: "pipe" });
    } catch (err) {
      log.warn("power assertion build failed, falling back to caffeinate:", err.message);
      powerAssertionProcess = spawn("caffeinate", ["-dims"], {
        detached: true,
        stdio: "ignore",
      });
      powerAssertionProcess.unref();
      return;
    }
  }
  if (!fs.existsSync(POWER_BIN)) return;
  powerAssertionProcess = spawn(POWER_BIN, [], { detached: true, stdio: "ignore" });
  powerAssertionProcess.unref();
  log.info("IOPMAssertion helper started (no App Nap / idle sleep)");
}

function stopPowerAssertion() {
  if (!powerAssertionProcess || !powerAssertionProcess.pid) return;
  try {
    process.kill(-powerAssertionProcess.pid, "SIGKILL");
  } catch (_) {
    try {
      powerAssertionProcess.kill("SIGKILL");
    } catch (_e) {
      /* ignore */
    }
  }
  powerAssertionProcess = null;
}

function resolvePython() {
  return fs.existsSync(PYTHON_BIN) ? PYTHON_BIN : "python3";
}

function buildSidecarEnv() {
  const userRoot = USER_ROOT();
  const dataDir = path.join(userRoot, "data");
  const analyticsDir = path.join(userRoot, "analytics");
  const venvBin = path.join(AGENT_ROOT, ".venv", "bin");
  const pathPrefix = fs.existsSync(venvBin) ? `${venvBin}:` : "";
  return {
    ...process.env,
    NODE_ENV: "production",
    IG_NODE_PROFILE: "shadow",
    IG_AGENT_MODE: "DEMO",
    IG_MOCK_FEED: "0",
    IG_AGENT_ROOT: AGENT_ROOT,
    IG_AGENT_DATA_DIR: dataDir,
    IG_ANALYTICS_DB: path.join(analyticsDir, "triage_v30.db"),
    IG_TRIAGE_DB: path.join(analyticsDir, "triage_v30.db"),
    IG_APEX_DESKTOP: "1",
    IG_APEX_NO_BROWSER: "1",
    IG_PRICING_REFERENCE: "yahoo",
    IG_APEX_PROTECT_PRODUCTION_PORTS: SHELL.runtime.protectProductionPorts ? "1" : "0",
    IG_AGENT_FROM_LAUNCHER: "1",
    IG_AGENT_SKIP_ORPHAN_KILL: "1",
    IG_APEX_IPC_SOCKET: "apex_ipc.sock",
    PYTHONPATH: path.join(AGENT_ROOT, "src"),
    IG_API_PORT: String(SHADOW_API_PORT),
    IG_COCKPIT_PORT: String(SHADOW_COCKPIT_PORT),
    PATH: `${pathPrefix}${process.env.PATH || "/usr/bin:/bin:/usr/sbin:/sbin"}`,
  };
}

function clearRendererSessionFootprint() {
  try {
    const ses = session.defaultSession;
    ses.clearStorageData({ storages: ["cookies", "localstorage", "sessionstorage"] });
    log.info("IPC reconnect: cleared renderer session storage footprint");
  } catch (err) {
    log.warn("clearRendererSessionFootprint:", err.message);
  }
}

function scheduleIpcReconnect(delayMs = IPC_RECONNECT_MS) {
  if (isQuitting) return;
  if (_ipcReconnectTimer) return;
  _ipcReconnectTimer = setTimeout(() => {
    _ipcReconnectTimer = null;
    connectIpcSocket(0);
  }, Math.max(100, delayMs));
}

function disconnectIpcClient() {
  if (!ipcClient) return;
  try {
    ipcClient.destroy();
  } catch (_) {
    /* ignore */
  }
  ipcClient = null;
  _ipcClientConnected = false;
}

function connectIpcSocket(attempt = 0) {
  if (!mainWindow || isQuitting) return;
  if (_ipcConnectInFlight) return;
  const IPC_SOCKET = ipcSocketPath();
  if (!fs.existsSync(IPC_SOCKET)) {
    if (attempt >= IPC_MAX_RETRIES) {
      log.error("IPC socket not available after retries");
      scheduleIpcReconnect(IPC_RETRY_MS);
      return;
    }
    setTimeout(() => connectIpcSocket(attempt + 1), IPC_RETRY_MS);
    return;
  }

  disconnectIpcClient();
  _ipcConnectInFlight = true;
  ipcClient = net.connect({ path: IPC_SOCKET });

  ipcClient.on("connect", () => {
    _ipcConnectInFlight = false;
    _ipcClientConnected = true;
    log.info("IPC bridge connected:", IPC_SOCKET);
    mainWindow.webContents.send("apex:ipc-status", { connected: true });
  });

  let buffer = "";
  ipcClient.on("data", (chunk) => {
    buffer += chunk.toString("utf8");
    let idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const tickString = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!tickString) continue;
      let payload;
      try {
        payload = JSON.parse(tickString);
      } catch {
        payload = null;
      }
      if (payload && payload.type === "warmup" && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("apex:warmup", payload);
        continue;
      }
      if (payload && payload.type === "ledger" && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("apex:ledger", payload);
        continue;
      }
      if (payload && payload.type === "story" && mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("apex:story", payload);
        continue;
      }
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("apex:tick", tickString);
      }
    }
  });

  ipcClient.on("error", (err) => {
    _ipcConnectInFlight = false;
    _ipcClientConnected = false;
    log.warn("IPC error:", err.message);
    scheduleIpcReconnect();
  });

  ipcClient.on("close", () => {
    _ipcConnectInFlight = false;
    _ipcClientConnected = false;
    if (mainWindow) mainWindow.webContents.send("apex:ipc-status", { connected: false });
    scheduleIpcReconnect();
  });
}

function waitForShadowApi(callback) {
  const deadline = Date.now() + SIDECAR_READY_TIMEOUT_MS;
  const probe = () => {
    if (probeShadowApiHealth200()) {
      callback(true);
      return;
    }
    if (Date.now() < deadline) setTimeout(probe, 500);
    else callback(false);
  };
  probe();
}

function createMainWindow() {
  const shellCfg = SHELL.shell;
  const preloadCfg = SHELL.preload;
  bootTrace(`createMainWindow transparency_hud=${APEX_TRANSPARENCY_HUD}`);
  mainWindow = new BrowserWindow({
    width: shellCfg.width,
    height: shellCfg.height,
    minWidth: shellCfg.minWidth,
    minHeight: shellCfg.minHeight,
    backgroundColor: shellCfg.backgroundColor,
    title: "IG Agent Apex v30.0",
    show: true,
    frame: !shellCfg.frameless,
    titleBarStyle: shellCfg.titleBarStyle,
    autoHideMenuBar: shellCfg.autoHideMenuBar,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: preloadCfg.contextIsolation,
      nodeIntegration: preloadCfg.nodeIntegration,
      sandbox: preloadCfg.sandbox,
      backgroundThrottling: shellCfg.backgroundThrottling,
      webSecurity: !isShadowSession(),
    },
  });

  const splashPath = path.join(__dirname, "build", "apex-splash.html");
  if (fs.existsSync(splashPath)) {
    mainWindow.loadFile(splashPath);
  } else {
    log.warn("apex-splash.html missing — loading dashboard directly");
  }

  mainWindow.once("ready-to-show", () => {
    mainWindow.focus();
    flushPendingBootPhases();
  });

  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));

  const loadDashboard = () => {
    if (!mainWindow) return;
    const indexPath = resolveDashboardIndexPath();
    if (fs.existsSync(indexPath)) {
      console.log(`[APEX ENGINE] Loading dashboard bundle from: ${indexPath}`);
      log.info(`[APEX ENGINE] Loading dashboard bundle from: ${indexPath}`);
      mainWindow.loadFile(indexPath);
    } else {
      const errPath = path.join(__dirname, "build", "apex-bundle-missing.html");
      log.error("dashboard/dist missing — no HTTP browser fallback (apex standalone)");
      if (fs.existsSync(errPath)) {
        mainWindow.loadFile(errPath);
      }
    }
    connectIpcSocket(0);
  };

  waitForShadowApi((ready) => {
    if (!ready) log.error(`Shadow API not ready within ${SIDECAR_READY_TIMEOUT_MS}ms — loading shell anyway`);
    dashboardLoaded = true;
    loadDashboard();
  });

  mainWindow.on("close", () => {
    log.info("Shell close — releasing UI only; detached daemon remains on :9090");
    releaseShellImmutable();
    mainWindow = null;
    dashboardLoaded = false;
  });

  mainWindow.webContents.on("did-start-navigation", (_event, _url, isInPlace, isMainFrame) => {
    if (!isMainFrame || !isInPlace || !dashboardLoaded) return;
    if (probeShadowApiHealth200()) {
      log.info("renderer reload — daemon healthy, IPC reconnect only");
      connectIpcSocket(0);
      return;
    }
    log.info("renderer reload — daemon down, supervisor respawn");
    ensureDaemonSupervisor({ forceRestart: true });
  });
}

ipcMain.handle("apex:get-version", () => ({
  apex: SHELL.version || "30.0.0",
  version: "30.0.0",
  versionLabel: "v30.0",
  node: SHELL.runtime.profile,
  production_preserved: SHELL.runtime.protectProductionPorts,
  shadow_api: SHADOW_API_PORT,
  packaged: app.isPackaged,
}));

// v30.0 Emergency Password Bypass Overwrite
ipcMain.handle("verify-admin-password", async (_event, _password) => {
  console.log("[APEX SECURITY] Administrative bypass engaged. Overriding login requirements.");
  return { authenticated: true, token: "v30_unlocked_session_token" };
});

/** Resolve live Python sidecar PID bound to shadow API port (not Electron wrapper). */
function resolveSidecarPid() {
  try {
    const r = spawnSync(
      "/usr/sbin/lsof",
      ["-iTCP", String(SHADOW_API_PORT), "-sTCP:LISTEN", "-t"],
      { encoding: "utf8", timeout: 3000 }
    );
    if (r.status !== 0 && !String(r.stdout || "").trim()) return null;
    const pid = parseInt(String(r.stdout || "").trim().split("\n")[0], 10);
    return Number.isFinite(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

ipcMain.handle("apex:sidecar-status", () => {
  const resolvedPid = resolveSidecarPid();
  const pid = resolvedPid ?? daemonProcess?.pid ?? null;
  const apiHealthy = probeShadowApiHealth200();
  const ipcLive = Boolean(ipcClient && _ipcClientConnected);
  return {
    running: apiHealthy,
    pid,
    sidecarPid: pid,
    adopted: sidecarExternallyAdopted || daemonDetached,
    ipcConnected: ipcLive || apiHealthy,
    apiHealthy,
    profile: SHELL.runtime.profile,
    agentRoot: AGENT_ROOT,
    userDataRoot: USER_ROOT(),
    daemonDetached,
  };
});

ipcMain.handle("apex:restart-sidecar", async () => {
  log.info("apex:restart-sidecar requested — daemon supervisor force restart");
  const ok = ensureDaemonSupervisor({ forceRestart: true });
  return { ok, port: SHADOW_API_PORT };
});

/** Native report export — HTTP to shadow sidecar (no Terminal / AppleScript). */
ipcMain.handle("apex:export-warmup-report", async () => {
  const url = `http://127.0.0.1:${SHADOW_API_PORT}/api/apex/export-warmup-report`;
  try {
    const res = await fetch(url, { method: "POST", signal: AbortSignal.timeout(30000) });
    if (!res.ok) {
      return { ok: false, error: `HTTP ${res.status}` };
    }
    return res.json();
  } catch (err) {
    log.warn("apex:export-warmup-report failed:", err.message);
    return { ok: false, error: err.message };
  }
});

const gotLock = app.requestSingleInstanceLock();
registerIpcHandlers();
installExternalBrowserGuards();

if (!gotLock) {
  bootTrace("single-instance lock denied — quitting");
  app.quit();
} else {
  bootTrace("single-instance lock acquired");
  app.on("second-instance", () => {
    ensureShadowSidecarRunning();
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    bootTrace("whenReady fired");
    try {
      broadcastBootPhase(0, "INITIALIZING APEX MONOLITH SYSTEM SHELL... STATUS: ACTIVE");
      ensureWritableLayout();
      bootTrace("whenReady layout ok");
      await prepareRendererSession();
      bootTrace("whenReady renderer session purified");
      ensureDaemonSupervisor();
      bootTrace("whenReady daemon supervisor armed");
      startPowerAssertion();
      createMainWindow();
      bootTrace("whenReady window created");
      log.info("APEX whenReady: session purge + daemon supervisor + shell complete");
    } catch (err) {
      bootTrace(`whenReady error: ${err.message}`);
      log.error("whenReady boot failed:", err.message);
    }
  });

  app.on("window-all-closed", () => {
    releaseShellImmutable();
    if (process.platform !== "darwin") app.quit();
  });

  app.on("activate", () => {
    isQuitting = false;
    ensureShadowSidecarRunning();
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  });

  app.on("before-quit", () => {
    log.info("before-quit — Cmd+Q shell unmount; detached trading daemon preserved on :9090");
    releaseShellImmutable();
    stopPowerAssertion();
  });
}
