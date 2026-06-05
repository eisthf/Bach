import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 백엔드(8000)로 API/WS 프록시. 프런트는 5173에서 동작.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 이 호스트는 localhost가 IPv6(::1)로만 해석됨 → 백엔드(IPv4)와 불일치.
      // 프록시 타깃을 127.0.0.1로 고정해 IPv4로 연결한다.
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
