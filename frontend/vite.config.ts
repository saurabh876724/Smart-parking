import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The dev server speaks the same language as the deployed one: the browser
  // calls its own origin, and these proxies forward to the API. That keeps CORS
  // out of the development loop entirely - configs/app.yaml ships an empty
  // allow-list and stays that way.
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
