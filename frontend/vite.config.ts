import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'
import tailwind from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwind()],
  server: {proxy: {'/api': 'http://127.0.0.1:8000', '/gift-preview': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000'}},
  build: {rollupOptions: {output: {manualChunks: {'charts': ['lightweight-charts']}}}},
})
