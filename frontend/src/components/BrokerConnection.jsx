import React from 'react'
import { useStore } from '../store'

const AUTH = { pending: '확인 중', refreshing: '갱신 중', ok: '정상', error: '실패 · 자동 재시도', expired: '만료 · 갱신 대기' }
const STREAM = { idle: '대기', connecting: '연결 중', connected: '연결됨', disconnected: '재연결 중', auth_error: '인증 대기' }

export default function BrokerConnection({ account }) {
  const { connected } = useStore()
  if (!account.live) return null
  const c = account.connection || {}
  const failed = ['error', 'expired'].includes(c.auth) || c.rest === 'error' || c.sync === 'error' || ['auth_error', 'disconnected'].includes(c.stream)
  const received = c.last_received_at && new Date(c.last_received_at)
  const timestamp = received && !Number.isNaN(received.getTime())
    ? received.toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    : null
  return (
    <div className={`broker-connection${failed || !connected ? ' warning' : ''}`} role="status">
      <strong>키움 {account.danger ? '실전' : '모의'}</strong>
      {!connected ? <span>앱 연결 끊김 · 증권사 상태 확인 불가</span> : <>
        <span>인증 {AUTH[c.auth] || '확인 중'}</span>
        <span>조회 {c.rest === 'ok' ? '정상' : c.rest === 'error' ? '실패' : '대기'}</span>
        <span>시세 {STREAM[c.stream] || '확인 중'}</span>
        {c.sync === 'syncing' && <span>잔고 확인 중</span>}
        {c.sync === 'error' && <span>잔고 확인 실패 · 재시도 중</span>}
        <span className="broker-last">{timestamp ? `최근 체결 수신 ${timestamp} (한국 시각)` : '체결 수신 대기'}</span>
      </>}
    </div>
  )
}
