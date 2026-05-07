import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    target: 'es2020',
    minify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-charts': ['lightweight-charts'],
          'vendor-react': ['react', 'react-dom'],
        },
      },
    },
    chunkSizeWarningLimit: 300,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8002',
      '/ws': { target: 'ws://127.0.0.1:8002', ws: true },
    }
  }
})
