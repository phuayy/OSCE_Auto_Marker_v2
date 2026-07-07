import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';
import { loadEnvFile } from './scripts/load-env.mjs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

loadEnvFile(__dirname);

const apiPort = Number(process.env.API_PORT || 8787);
const apiTarget = `http://localhost:${apiPort}`;

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
    proxy: {
      '/api': makeProxyOptions(apiTarget),
      '/media': makeProxyOptions(apiTarget),
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src')
    }
  }
});
