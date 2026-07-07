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
const pythonBin = process.env.PYTHON_BIN || 'python';
const DEFAULT_API_PORT = Number(process.env.API_PORT || 8787);
const PORT_SCAN_LIMIT = 20;

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
  const child = spawn(command, args, {
    cwd: rootDir,
    stdio: 'inherit',
    env: {
      ...process.env,
      ...extraEnv,
    },
    windowsHide: false,
  });

  child.on('error', (error) => {
    processExitCode = 1;
    console.error(`[${name}] Failed to start: ${error.message}`);
    shutdownAll();
  });

  child.on('exit', (code, signal) => {
    if (shuttingDown) {
      return;
    }

    if (code !== 0) {
      processExitCode = code || 1;
      console.error(`[${name}] exited with code ${code}${signal ? ` (signal: ${signal})` : ''}`);
    }

    shutdownAll(name);
  });

  children.push({ name, child });
  return child;
}

function shutdownAll(exceptName = null) {
  if (shuttingDown) {
    return;
  }

  shuttingDown = true;

  for (const entry of children) {
    if (entry.name === exceptName) {
      continue;
    }

    if (entry.child.exitCode === null) {
      entry.child.kill('SIGTERM');
    }
  }

  // Give child processes a brief moment to shut down gracefully.
  setTimeout(() => {
    for (const entry of children) {
      if (entry.child.exitCode === null) {
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
    pythonBin,
    [
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
