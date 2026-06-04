import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 백엔드(8000)로 API/WS 프록시. 프런트는 5173에서 동작.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
