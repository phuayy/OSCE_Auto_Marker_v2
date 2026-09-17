// Where the dev tooling listens and where it proxies to, from the same .env
// the API reads. Pure: no process.env access in here, so test/devHosts.test.mjs
// can pin the rules without touching the environment.
//
// Two different questions share one variable:
//   API_HOST is what uvicorn *binds*. "0.0.0.0" means every interface, which
//   is a valid bind address and an invalid one to connect to — so the proxy
//   target derived from it must be a loopback address the browser's dev
//   server can actually dial.
//   DEV_SERVER_HOST is what Vite binds. Loopback by default (nothing else on
//   the network sees a half-set-up dev server); "0.0.0.0" to try the dev
//   build from a phone on the same Wi-Fi.

export const DEFAULT_API_HOST = '127.0.0.1';
export const DEFAULT_API_PORT = 8787;
export const DEFAULT_DEV_SERVER_HOST = '127.0.0.1';
export const DEFAULT_DEV_SERVER_PORT = 5173;
export const DEFAULT_PREVIEW_PORT = 4173;

// Bind addresses that mean "all interfaces" and cannot be connected to.
const WILDCARD_HOSTS = new Set(['', '0.0.0.0', '::', '[::]']);

function readPort(value, fallback) {
  const parsed = Number.parseInt(String(value ?? '').trim(), 10);
  return Number.isInteger(parsed) && parsed > 0 && parsed < 65536 ? parsed : fallback;
}

function readHost(value, fallback) {
  const trimmed = String(value ?? '').trim();
  return trimmed || fallback;
}

/** The host a client on this machine dials to reach a server bound at `bindHost`. */
export function connectableHost(bindHost) {
  const host = String(bindHost ?? '').trim();
  if (WILDCARD_HOSTS.has(host)) {
    return DEFAULT_API_HOST;
  }
  // A bare IPv6 literal needs brackets inside a URL.
  if (host.includes(':') && !host.startsWith('[')) {
    return `[${host}]`;
  }
  return host;
}

/** Origin the Vite proxy forwards /api and /media to. */
export function apiProxyTarget(env) {
  const host = connectableHost(readHost(env.API_HOST, DEFAULT_API_HOST));
  const port = readPort(env.API_PORT, DEFAULT_API_PORT);
  return `http://${host}:${port}`;
}

/** Vite's own `server` / `preview` bind settings. */
export function devServerBind(env) {
  return {
    host: readHost(env.DEV_SERVER_HOST, DEFAULT_DEV_SERVER_HOST),
    port: readPort(env.DEV_SERVER_PORT, DEFAULT_DEV_SERVER_PORT),
    previewPort: readPort(env.PREVIEW_PORT, DEFAULT_PREVIEW_PORT),
  };
}
