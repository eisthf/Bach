// 최소 해시 라우터.
//
// react-router를 넣지 않는 이유: 라우트가 둘뿐이고, 이 앱은 정적 파일 서버 없이
// Vite 개발 서버로만 뜬다. 해시(#/...)는 서버 재작성 설정 없이도 새로고침과
// 뒤로/앞으로가 그대로 동작한다.
import { useEffect, useState } from 'react'

export const ROUTES = {
  TRADING: '/',
  UPPER_LIMIT: '/upper-limit',
}

function currentPath() {
  const h = window.location.hash.replace(/^#/, '')
  return h.startsWith('/') ? h : ROUTES.TRADING
}

export function useRoute() {
  const [route, setRoute] = useState(currentPath)
  useEffect(() => {
    const onChange = () => setRoute(currentPath())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return route
}

export function navigate(path) {
  window.location.hash = path
}
