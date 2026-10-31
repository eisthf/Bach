// 화면 폭 선택(모든 페이지 공통): 전체 폭 / 가운데(양쪽 여백, 최대 1440px).
// <html data-layout="center"> 로 CSS가 갈린다. 선택은 브라우저에 기억한다.
import React, { useState } from 'react'

const KEY = 'bach.layout'
const read = () => { try { return localStorage.getItem(KEY) === 'center' ? 'center' : 'wide' } catch { return 'wide' } }
const apply = (v) => { document.documentElement.dataset.layout = v }

// 첫 그리기 전에 적용해 화면이 한 번 넓었다 좁아지는 깜빡임을 막는다(모듈을 읽는 순간).
apply(read())

export default function LayoutToggle() {
  const [layout, setLayout] = useState(read)
  const choose = (v) => {
    setLayout(v)
    apply(v)
    try { localStorage.setItem(KEY, v) } catch { /* 저장 불가면 이번 화면만 */ }
  }
  return (
    <div className="layout-toggle" role="group" aria-label="화면 폭">
      <button type="button" className={layout === 'wide' ? 'on' : ''} aria-pressed={layout === 'wide'}
        title="창 폭 전체를 씁니다" onClick={() => choose('wide')}>전체 폭</button>
      <button type="button" className={layout === 'center' ? 'on' : ''} aria-pressed={layout === 'center'}
        title="가운데 1440px만 쓰고 양쪽은 여백으로 둡니다" onClick={() => choose('center')}>가운데</button>
    </div>
  )
}
