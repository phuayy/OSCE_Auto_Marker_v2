import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import net from 'node:net';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { loadEnvFile } from './load-env.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

loadEnvFile(rootDir);

const backendAppDir = path.join(rootDir, 'fastapi_backend');
const viteBin = path.join(rootDir, 'node_modules', 'vite', 'bin', 'vite.js');
// uv owns the Python environment. "uv run" resolves the project's .venv itself,
// so the dev server does not depend on an activated shell or on whichever
// interpreter happens to be first on PATH — a bare "python" here picked up the
// system install and started the API against the wrong dependency set.
// PYTHON_BIN still overrides it for a hand-managed interpreter.
const pythonOverride = process.env.PYTHON_BIN;
const apiCommand = pythonOverride || (process.platform === 'win32' ? 'uv.exe' : 'uv');
const apiCommandPrefix = pythonOverride ? [] : ['run', '--no-sync', 'python'];
const DEFAULT_API_PORT = Number(process.env.API_PORT || 8787);
const PORT_SCAN_LIMIT = 20;

// One dev process dying should not take the other down with it — a crashed API
// on a bad import is a two-second fix, and killing Vite forces a full restart
// (and a browser reload) for nothing. Restart it instead, and only give up when
// it keeps dying immediately, which means it cannot start at all.
const RESTART_DELAY_MS = 500;
const RAPID_FAILURE_WINDOW_MS = 5000;
const MAX_RAPID_FAILURES = 3;

if (!existsSync(backendAppDir)) {
  console.error('Missing FastAPI backend directory: fastapi_backend');
  process.exit(1);
}

if (!existsSync(viteBin)) {
  console.error('Missing Vite binary. Run npm install first.');
  process.exit(1);
}

const children = [];
let shuttingDown = false;
let processExitCode = 0;

function isPortAvailable(port) {
  return new Promise((resolve) => {
    const tester = net.createServer();

    tester.once('error', (error) => {
      if (error.code === 'EADDRINUSE' || error.code === 'EACCES') {
        resolve(false);
        return;
      }

      resolve(false);
    });

    tester.once('listening', () => {
      tester.close(() => resolve(true));
    });

    // Match Express default binding behavior to avoid false positives on Windows IPv6.
    tester.listen(port);
  });
}

async function resolveApiPort() {
  const preferredPort = Number.isInteger(DEFAULT_API_PORT) && DEFAULT_API_PORT > 0 ? DEFAULT_API_PORT : 8787;

  if (await isPortAvailable(preferredPort)) {
    return { selectedPort: preferredPort, preferredPort };
  }

  for (let offset = 1; offset <= PORT_SCAN_LIMIT; offset += 1) {
    const candidate = preferredPort + offset;
    if (await isPortAvailable(candidate)) {
      return { selectedPort: candidate, preferredPort };
    }
  }

  throw new Error(`Could not find an open API port between ${preferredPort} and ${preferredPort + PORT_SCAN_LIMIT}.`);
}

function launch(name, command, args, extraEnv = {}) {
  const entry = {
    name,
    command,
    args,
    extraEnv,
    child: null,
    startedAt: 0,
    rapidFailures: 0,
  };

  children.push(entry);
  spawnChild(entry);
  return entry;
}

function spawnChild(entry) {
  const child = spawn(entry.command, entry.args, {
    cwd: rootDir,
    stdio: 'inherit',
    env: {
      ...process.env,
      ...entry.extraEnv,
    },
    windowsHide: false,
  });

  entry.child = child;
  entry.startedAt = Date.now();

  child.on('error', (error) => {
    // The command itself could not be run (missing binary, bad path). Retrying
    // cannot help, so this is the one case that still stops everything.
    processExitCode = 1;
    console.error(`[${entry.name}] Failed to start: ${error.message}`);
    shutdownAll();
  });

  child.on('exit', (code, signal) => {
    if (shuttingDown) {
      return;
    }

    const ranForMs = Date.now() - entry.startedAt;
    entry.rapidFailures = ranForMs < RAPID_FAILURE_WINDOW_MS ? entry.rapidFailures + 1 : 0;

    const reason = `code ${code}${signal ? ` (signal: ${signal})` : ''}`;

    if (entry.rapidFailures > MAX_RAPID_FAILURES) {
      processExitCode = code || 1;
      console.error(`[${entry.name}] exited with ${reason} and keeps failing on startup. Stopping.`);
      shutdownAll(entry.name);
      return;
    }

    console.warn(`[${entry.name}] exited with ${reason}. Restarting...`);
    setTimeout(() => {
      if (!shuttingDown) {
        spawnChild(entry);
      }
    }, RESTART_DELAY_MS);
  });

  return child;
}

function shutdownAll(exceptName = null) {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;

  for (const entry of children) {
    if (entry.name === exceptName || entry.child === null) {
      continue;
    }

    if (entry.child.exitCode === null) {
      entry.child.kill('SIGTERM');
    }
  }

  // Give child processes a brief moment to shut down gracefully.
  setTimeout(() => {
    for (const entry of children) {
      if (entry.child !== null && entry.child.exitCode === null) {
        entry.child.kill('SIGKILL');
      }
    }
    process.exit(processExitCode);
  }, 350);
}

process.on('SIGINT', () => {
  processExitCode = 0;
  shutdownAll();
});

process.on('SIGTERM', () => {
  processExitCode = 0;
  shutdownAll();
});

async function startDevProcesses() {
  const { selectedPort, preferredPort } = await resolveApiPort();

  if (selectedPort !== preferredPort) {
    console.warn(`[dev] Port ${preferredPort} is busy. Using API port ${selectedPort} instead.`);
  }

  const sharedEnv = {
    API_PORT: String(selectedPort),
  };

  launch(
    'api',
    apiCommand,
    [
      ...apiCommandPrefix,
      'scripts/run_api.py',
      '--reload',
      '--port',
      String(selectedPort),
    ],
    sharedEnv,
  );
  launch('client', process.execPath, [viteBin], sharedEnv);
}

startDevProcesses().catch((error) => {
  console.error(`[dev] ${error.message}`);
  process.exit(1);
});
