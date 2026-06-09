import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Forward API + health checks to the backend (FastAPI/uvicorn on :8787).
// changeOrigin so the Host header matches the target; no path rewrite —
// the backend already serves these exact paths.
// Shared by dev server (:5173) and `vite preview` (:4173) so both work
// out-of-the-box with the default relative API_BASE.
const apiProxy = {
  '/v1': {
    target: 'http://127.0.0.1:8787',
    changeOrigin: true,
  },
  '/health': {
    target: 'http://127.0.0.1:8787',
    changeOrigin: true,
  },
};

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: apiProxy,
  },
  preview: {
    port: 4173,
    proxy: apiProxy,
  },
});
