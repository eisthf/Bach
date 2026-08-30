import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'

// 백엔드 .env 의 API_TOKEN 을 읽는다(설정돼 있으면 프록시가 인증 헤더를 주입).
// 토큰이 브라우저에 내려가지 않고 프록시(서버 측)에만 머무는 게 요점 —
// 프런트 번들에 넣으면 페이지를 여는 누구나 토큰을 읽을 수 있다.
function backendToken() {
  try {
    const env = fs.readFileSync(path.resolve(__dirname, '../backend/.env'), 'utf-8')
    const m = env.match(/^\s*API_TOKEN\s*=\s*(.+?)\s*$/m)
    return m ? m[1] : ''
  } catch {
    return ''
  }
}

// 백엔드(8000)로 API/WS 프록시. 프런트는 5173에서 동작.
export default defineConfig(() => {
  const token = backendToken()
  return {
    plugins: [react()],
    server: {
      // 모든 인터페이스(IPv4 0.0.0.0 포함)에 바인딩. 기본값은 IPv6 ::1 전용이라
      // VSCode 포트포워딩(127.0.0.1 IPv4)이 연결하지 못해 접속이 멈춘다.
      host: true,
      port: 5173,
      proxy: {
        // 이 호스트는 localhost가 IPv6(::1)로만 해석됨 → 백엔드(IPv4)와 불일치.
        // 프록시 타깃을 127.0.0.1로 고정해 IPv4로 연결한다.
        '/api': {
          target: 'http://127.0.0.1:8000',
          headers: token ? { 'X-API-Token': token } : {},
        },
        '/ws': {
          target: 'ws://127.0.0.1:8000',
          ws: true,
          // WebSocket 은 커스텀 헤더를 못 실으므로 쿼리로 전달.
          rewrite: token
            ? (p) => p + (p.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token)
            : undefined,
        },
      },
    },
  }
})
