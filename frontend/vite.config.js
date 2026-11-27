import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api/v1/data': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true
      },
      '/api/benchmark': {
        target: 'http://127.0.0.1:8002',
        changeOrigin: true
      },
      '/api/eval': {
        target: 'http://127.0.0.1:8002',
        changeOrigin: true
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  }
})
