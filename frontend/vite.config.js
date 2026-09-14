import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The browser talks only to its own origin. `/api` is proxied to the
// Flask search engine by the dev server, so the UI works unchanged
// locally and behind any proxy or remote preview.
//
// Calling http://127.0.0.1:5000 from the browser would break as soon as
// the page is not served from the same machine as the API.
const API_TARGET = process.env.SEARCH_ENGINE_API_URL || 'http://127.0.0.1:5000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // The preview host is not known in advance, so accept it.
    allowedHosts: true,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
})
