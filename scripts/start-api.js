/* API-only startup: app_data dirs, userConfig from env, uvicorn. Does not start Next or nginx. */

import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { spawn } from "child_process";
import { randomUUID } from "crypto";
import { request } from "http";
import {
  chmodSync,
  closeSync,
  copyFileSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "fs";
import { printGSlideStartupBanner } from "./gslide-terminal-banner.mjs";
import userConfigEnv from "./user-config-env.cjs";

const { buildUserConfigFromEnv, readUserConfigEnv } = userConfigEnv;

process.umask(0o022);

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const appRoot = join(__dirname, "..");
const fastapiDir = join(appRoot, "servers/fastapi");
const exportSyncScript = join(__dirname, "sync-presentation-export.cjs");

const args = process.argv.slice(2);
const isDev = args.includes("--dev") || args.includes("-d");
const fastapiPort = 8000;

const appDataDirectory = process.env.APP_DATA_DIRECTORY;
if (!appDataDirectory) {
  throw new Error("APP_DATA_DIRECTORY is required");
}

const appDataDirectoryMode = 0o755;
const userConfigPath = join(appDataDirectory, "userConfig.json");
const userConfigBackupPath = `${userConfigPath}.bak`;
const userDataDir = dirname(userConfigPath);
const appDataStaticDirectories = [
  "exports",
  "images",
  "uploads",
  "fonts",
  "templates",
  "pptx-to-html",
  "pptx-to-json",
].map((name) => join(appDataDirectory, name));

const chmodBestEffort = (path, mode) => {
  try {
    chmodSync(path, mode);
  } catch (error) {
    if (error && (error.code === "EPERM" || error.code === "EACCES")) {
      return;
    }
    throw error;
  }
};

const ensureReadableDirectory = (dirPath) => {
  mkdirSync(dirPath, { recursive: true, mode: appDataDirectoryMode });
  chmodBestEffort(dirPath, appDataDirectoryMode);
};

const ensureReadableExportFiles = (dirPath) => {
  if (!existsSync(dirPath)) {
    return;
  }
  for (const entry of readdirSync(dirPath, { withFileTypes: true })) {
    const entryPath = join(dirPath, entry.name);
    if (entry.isDirectory()) {
      chmodBestEffort(entryPath, appDataDirectoryMode);
      ensureReadableExportFiles(entryPath);
    } else if (entry.isFile()) {
      chmodBestEffort(entryPath, 0o644);
    }
  }
};

const ensureAppDataDirectories = () => {
  ensureReadableDirectory(userDataDir);
  for (const dirPath of appDataStaticDirectories) {
    ensureReadableDirectory(dirPath);
  }
  ensureReadableExportFiles(join(appDataDirectory, "exports"));
};

ensureAppDataDirectories();

const readJsonConfig = (filePath) => {
  try {
    if (!existsSync(filePath)) {
      return undefined;
    }
    const raw = readFileSync(filePath, "utf8").trim();
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : undefined;
  } catch {
    return undefined;
  }
};

const readUserConfig = () =>
  readJsonConfig(userConfigPath) || readJsonConfig(userConfigBackupPath) || {};

const copyUserConfigBackup = () => {
  try {
    if (readJsonConfig(userConfigPath)) {
      copyFileSync(userConfigPath, userConfigBackupPath);
      chmodBestEffort(userConfigBackupPath, 0o644);
    }
  } catch (error) {
    console.warn("Failed to update user config backup:", error);
  }
};

const writeUserConfig = (config) => {
  ensureReadableDirectory(userDataDir);
  copyUserConfigBackup();

  const tempPath = `${userConfigPath}.${process.pid}.${Date.now()}.${randomUUID()}.tmp`;
  const fd = openSync(tempPath, "w");
  try {
    writeFileSync(fd, JSON.stringify(config), "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }

  try {
    renameSync(tempPath, userConfigPath);
  } catch (error) {
    try {
      unlinkSync(tempPath);
    } catch {
      // Best-effort cleanup.
    }
    throw error;
  }

  chmodBestEffort(userConfigPath, 0o644);
  if (!existsSync(userConfigBackupPath)) {
    copyUserConfigBackup();
  }
};

const runCommand = (command, commandArgs, options = {}) => {
  return new Promise((resolve, reject) => {
    const child = spawn(command, commandArgs, {
      cwd: options.cwd || appRoot,
      stdio: options.stdio || "inherit",
      env: options.env || process.env,
    });

    child.on("error", (err) => {
      reject(err);
    });

    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`${command} exited with code: ${code}`));
      }
    });
  });
};

const runNodeScript = (scriptPath, scriptArgs) => {
  return runCommand(process.execPath, [scriptPath, ...scriptArgs], {
    cwd: appRoot,
  });
};

const ensurePresentationExportRuntime = async () => {
  if (process.env.ENSURE_PRESENTATION_EXPORT_RUNTIME === "false") {
    return;
  }

  if (!existsSync(exportSyncScript)) {
    console.warn("presentation-export sync script not found; skipping runtime check");
    return;
  }

  try {
    await runNodeScript(exportSyncScript, ["--check-only"]);
  } catch {
    if (!isDev) {
      throw new Error(
        "presentation-export runtime is missing in this container image. Rebuild the image so the runtime package is installed."
      );
    }

    console.warn("presentation-export runtime missing in dev mount. Syncing runtime package...");
    await runNodeScript(exportSyncScript, ["--force"]);
  }
};

process.env.USER_CONFIG_PATH = userConfigPath;

const setupUserConfigFromEnv = () => {
  const existingConfig = readUserConfig();
  const envConfig = readUserConfigEnv(process.env);
  if (Object.keys(existingConfig).length > 0 && Object.keys(envConfig).length === 0) {
    return;
  }
  writeUserConfig(buildUserConfigFromEnv(existingConfig, process.env));
};

const waitForProcessHttp = (
  processName,
  childProcess,
  port,
  path,
  host = "127.0.0.1",
  timeoutMs = 60_000
) => {
  return new Promise((resolve, reject) => {
    let isSettled = false;
    let retryTimer;
    const deadline = Date.now() + timeoutMs;

    const settle = (callback, value) => {
      if (isSettled) {
        return;
      }
      isSettled = true;
      if (retryTimer) {
        clearTimeout(retryTimer);
      }
      callback(value);
    };

    const retry = () => {
      if (Date.now() >= deadline) {
        settle(
          reject,
          new Error(
            `${processName} did not respond at http://${host}:${port}${path} within ${timeoutMs}ms`
          )
        );
        return;
      }
      retryTimer = setTimeout(checkHttp, 250);
    };

    const checkHttp = () => {
      let handled = false;
      const req = request(
        { host, port, path, method: "GET", timeout: 1_000 },
        (response) => {
          response.resume();
          finishAttempt(true);
        }
      );

      const finishAttempt = (isReady) => {
        if (handled) {
          return;
        }
        handled = true;
        req.destroy();
        if (isReady) {
          settle(resolve);
        } else if (!isSettled) {
          retry();
        }
      };

      req.once("error", () => finishAttempt(false));
      req.once("timeout", () => finishAttempt(false));
      req.end();
    };

    childProcess.on("exit", (code) => {
      if (!isSettled) {
        settle(
          reject,
          new Error(
            `${processName} exited before responding at http://${host}:${port}${path} (exit code: ${code})`
          )
        );
      }
    });

    childProcess.on("error", (err) => {
      if (!isSettled) {
        settle(reject, err);
      }
    });

    checkHttp();
  });
};

const main = async () => {
  await ensurePresentationExportRuntime();
  setupUserConfigFromEnv();

  let isShuttingDown = false;
  let fastApiProcess;
  const shutdown = (exitCode) => {
    if (isShuttingDown) {
      return;
    }
    isShuttingDown = true;
    if (fastApiProcess && !fastApiProcess.killed) {
      fastApiProcess.kill();
    }
    process.exit(exitCode);
  };

  process.once("SIGINT", () => shutdown(0));
  process.once("SIGTERM", () => shutdown(0));

  fastApiProcess = spawn(
    "python",
    [
      "server.py",
      "--host",
      "0.0.0.0",
      "--port",
      fastapiPort.toString(),
      "--reload",
      isDev ? "true" : "false",
    ],
    {
      cwd: fastapiDir,
      stdio: "inherit",
      env: process.env,
    }
  );

  fastApiProcess.on("error", (err) => {
    console.error("FastAPI process failed to start:", err);
  });

  fastApiProcess.on("exit", (code, signal) => {
    if (isShuttingDown) {
      return;
    }
    const exitCode = typeof code === "number" ? code : signal ? 1 : 0;
    console.error(
      `FastAPI process exited. Exit code: ${exitCode}${
        signal ? `, signal: ${signal}` : ""
      }`
    );
    shutdown(exitCode);
  });

  try {
    await waitForProcessHttp(
      "FastAPI",
      fastApiProcess,
      fastapiPort,
      "/api/v1/auth/status"
    );
    printGSlideStartupBanner({
      mode: isDev ? "development" : "production",
      nextPort: 3000,
      fastapiPort,
    });
  } catch (err) {
    console.warn(`Skipping startup banner: ${err.message}`);
  }

  await new Promise(() => {});
};

main();
