const { app, BrowserWindow, globalShortcut, shell, Menu } = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

const API_PORT = process.env.SCHOLARFLOW_API_PORT || 8765;
const WEB_PORT = process.env.SCHOLARFLOW_WEB_PORT || 3000;
const API_URL = `http://127.0.0.1:${API_PORT}`;
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

let mainWindow = null;
let backendProcess = null;
let isQuitting = false;

const repoRoot = path.resolve(__dirname, "../../..");
const workbenchScript = path.join(repoRoot, "scripts", "workbench.ps1");
const iconPath = path.join(__dirname, "..", "public", "brand", "scholarflow-icon.ico");

function checkEndpoint(url, timeout = 1500) {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout }, (res) => {
      resolve(res.statusCode >= 200 && res.statusCode < 400);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function ensureBackendRunning() {
  const isApiReady = await checkEndpoint(`${API_URL}/api/health`);
  const isWebReady = await checkEndpoint(WEB_URL);

  if (isApiReady && isWebReady) {
    console.log("[ScholarFlow Desktop] Core services already active on ports", API_PORT, WEB_PORT);
    return true;
  }

  console.log("[ScholarFlow Desktop] Launching local workbench runtime via workbench.ps1...");
  try {
    backendProcess = spawn(
      "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        workbenchScript,
        "-Mode",
        "production",
        "-ApiPort",
        String(API_PORT),
        "-WebPort",
        String(WEB_PORT),
      ],
      {
        cwd: repoRoot,
        windowsHide: true,
        stdio: "ignore",
      }
    );

    backendProcess.on("exit", (code) => {
      console.log(`[ScholarFlow Desktop] Backend runner exited with code ${code}`);
      backendProcess = null;
    });
  } catch (err) {
    console.error("[ScholarFlow Desktop] Failed to spawn backend process:", err);
  }

  return false;
}

function getSplashHtml() {
  return `data:text/html;charset=utf-8,${encodeURIComponent(`
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
      <meta charset="utf-8" />
      <title>ScholarFlow Research Workbench</title>
      <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          height: 100vh;
          background: radial-gradient(circle at 50% 20%, #1e1e24 0%, #09090b 80%);
          color: #f4f4f5;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", sans-serif;
          user-select: none;
          overflow: hidden;
        }
        .logo-wrap {
          position: relative;
          width: 72px;
          height: 72px;
          display: grid;
          place-items: center;
          margin-bottom: 24px;
        }
        .logo-glow {
          position: absolute;
          inset: -8px;
          border-radius: 24px;
          background: radial-gradient(circle, rgba(37, 99, 235, 0.4) 0%, transparent 70%);
          filter: blur(12px);
          animation: pulse 2.4s ease-in-out infinite;
        }
        .logo-box {
          position: relative;
          z-index: 1;
          width: 64px;
          height: 64px;
          display: grid;
          place-items: center;
          border-radius: 18px;
          background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
          box-shadow: 0 12px 32px rgba(37, 99, 235, 0.35);
          font-size: 32px;
          font-weight: 800;
          color: #ffffff;
        }
        h1 {
          font-size: 20px;
          font-weight: 700;
          letter-spacing: -0.01em;
          margin-bottom: 8px;
        }
        p {
          font-size: 13px;
          color: #a1a1aa;
          margin-bottom: 28px;
        }
        .spinner {
          width: 28px;
          height: 28px;
          border: 3px solid rgba(255, 255, 255, 0.12);
          border-top-color: #3b82f6;
          border-radius: 50%;
          animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        @keyframes pulse { 50% { opacity: 0.5; transform: scale(1.08); } }
      </style>
    </head>
    <body>
      <div class="logo-wrap">
        <div class="logo-glow"></div>
        <div class="logo-box">S</div>
      </div>
      <h1>ScholarFlow Research Workbench</h1>
      <p id="status-text">正在启动学术工作台与 Pi 智能体服务...</p>
      <div class="spinner"></div>
    </body>
    </html>
  `)}`;
}

function createWindow() {
  Menu.setApplicationMenu(null); // Clean frameless native look

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1080,
    minHeight: 700,
    title: "ScholarFlow Research Workbench",
    icon: iconPath,
    backgroundColor: "#09090b",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      spellcheck: true,
    },
  });

  mainWindow.loadURL(getSplashHtml());
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  // Intercept external links to open in the system default browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http:") || url.startsWith("https:")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  // Poll until Web ready, then load the real UI
  const pollInterval = setInterval(async () => {
    const ready = await checkEndpoint(WEB_URL);
    if (ready && mainWindow && !mainWindow.isDestroyed()) {
      clearInterval(pollInterval);
      mainWindow.loadURL(WEB_URL);
    }
  }, 800);

  // Global dev shortcuts inside the window
  mainWindow.webContents.on("before-input-event", (event, input) => {
    if (input.key === "F12" || (input.control && input.shift && input.key.toLowerCase() === "i")) {
      mainWindow.webContents.toggleDevTools();
      event.preventDefault();
    } else if (input.key === "F5" || (input.control && input.key.toLowerCase() === "r")) {
      mainWindow.webContents.reload();
      event.preventDefault();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  await ensureBackendRunning();
  createWindow();

  // Register Alt+Space to toggle / focus ScholarFlow window
  try {
    globalShortcut.register("Alt+Space", () => {
      if (!mainWindow) return;
      if (mainWindow.isMinimized()) {
        mainWindow.restore();
        mainWindow.focus();
      } else if (!mainWindow.isVisible()) {
        mainWindow.show();
        mainWindow.focus();
      } else if (mainWindow.isFocused()) {
        mainWindow.minimize();
      } else {
        mainWindow.focus();
      }
    });
  } catch (err) {
    console.warn("[ScholarFlow Desktop] Could not register global shortcut:", err);
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
