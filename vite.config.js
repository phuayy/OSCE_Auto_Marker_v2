import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';
import { loadEnvFile } from './scripts/load-env.mjs';
import { apiProxyTarget, devServerBind } from './scripts/dev-hosts.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

loadEnvFile(__dirname);

// Bind and proxy addresses come from the same .env the API reads
// (API_HOST / API_PORT / DEV_SERVER_HOST / DEV_SERVER_PORT / PREVIEW_PORT);
// the rules live in scripts/dev-hosts.mjs.
const apiTarget = apiProxyTarget(process.env);
const bind = devServerBind(process.env);

function makeProxyOptions(target) {
  return {
    target,
    changeOrigin: true,
    configure(proxy) {
      proxy.on('error', (err, req, res) => {
        console.error('[vite proxy error]', err.code, req.url);
        if (!res.headersSent) {
          res.writeHead(502, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ detail: 'Proxy error: backend unavailable.' }));
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [react()],
  server: {
    // Loopback by default, and IPv4 explicitly: Node 17+ otherwise binds ::1
    // only, which refuses clients that resolve localhost to 127.0.0.1.
    // DEV_SERVER_HOST=0.0.0.0 opens the dev build to the local network.
    host: bind.host,
    port: bind.port,
    // Fail loudly instead of silently sliding to the next port when taken.
    strictPort: true,
    watch: {
      // Vite's watcher ignores only .git, node_modules and the build output,
      // so on this repo chokidar walked the Python virtualenv (~59k files in
      // ~9.3k directories once torch and NeMo are installed) and the runtime
      // artefact store before the dev server was usable: measured 256 s and
      // ~780 MB of watcher state, against 2 s with these out. None of them
      // holds anything Vite serves. Same scoping run_api.py applies to its own
      // reloader (RELOAD_DIRS). Appended to Vite's defaults, not replacing them.
      // public/ is deliberately not listed: the watcher is what keeps the
      // served public-file set current when a file is added there.
      ignored: [
        '**/.venv/**',
        '**/storage/**',
        '**/__pycache__/**',
        '**/.codegraph/**',
        '**/.ruff_cache/**',
        '**/fastapi_backend/**',
      ],
    },
    proxy: {
      '/api': makeProxyOptions(apiTarget),
      '/media': makeProxyOptions(apiTarget),
    },
  },
  // `vite preview` serves dist/ the way a static host would, with the same
  // proxy (preview.proxy defaults to server.proxy) — a quick check of a
  // production build before SERVE_FRONTEND / a reverse proxy takes over.
  preview: {
    host: bind.host,
    port: bind.previewPort,
    strictPort: true,
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src')
    }
  },
  build: {
    rollupOptions: {
      output: {
        // Vendor code changes on a dependency bump; application code changes
        // every deploy. Splitting them means a UI fix does not invalidate the
        // ~300 kB of React/motion/icons a returning browser already holds.
        // Route chunks are produced by the dynamic imports in the app itself
        // (see src/lib/lazyRoute.jsx) — this map only covers node_modules.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined;
          if (/[\/]node_modules[\/](react|react-dom|scheduler)[\/]/.test(id)) return 'vendor-react';
          if (id.includes('framer-motion') || id.includes('motion-dom') || id.includes('motion-utils')) {
            return 'vendor-motion';
          }
          if (id.includes('lucide-react')) return 'vendor-icons';
          return 'vendor';
        },
      },
    },
  }
});
