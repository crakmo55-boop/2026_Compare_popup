# -*- coding: utf-8 -*-
"""Landing / PF Popup 이 공통으로 쓰는 Compare 컴포넌트(.pdd43-master-compare) 읽기.

JS 는 기존 Key spec 코드(Compare-main/compare/run_c_landing_live.py, run_popup_ui_live.py)의
JS_STATE / JS_COLUMN / JS_POP_COLUMN 판정 방식을 그대로 쓴다.
  - Key Specs 섹션 노출: .pdd43-master-compare__feature 가 화면에 보이는지
  - 제품(열)별 Key Specs 값: feature-item--N 의 값이 비어 있지 않고 '-' 가 아니면 '값 있음'
  - 텍스트 정규화: 기존 코드와 같은 공백 압축(replace(/\\s+/g, ' ').trim()) 만 사용
"""
import logging

from common.config import RESULT_FAIL, RESULT_PASS

# 기존 run_c_landing_live.JS_STATE 와 같은 판정 (cols: 열 번호 → 값 있는/없는 항목 수)
JS_STATE = r"""() => {
  const root = document.querySelector('.pdd43-master-compare');
  if (!root) return {hasRoot: false};
  const vis = e => !!(e && e.offsetParent !== null && e.getBoundingClientRect().height > 0);
  const feat = root.querySelector('.pdd43-master-compare__feature');
  const head = feat && feat.querySelector('.pdd43-master-compare__sub-headline');
  const lists = feat ? [...feat.querySelectorAll('ul.pdd43-master-compare__feature-list')] : [];
  const cols = {};
  for (const ul of lists) {
    for (const li of ul.querySelectorAll('li.pdd43-master-compare__feature-item')) {
      const m = li.className.match(/feature-item--(\d+)/); const c = m ? m[1] : '?';
      const v = (li.querySelector('.pdd43-master-compare__feature-value') || {}).innerText || '';
      cols[c] = cols[c] || {filled: 0, empty: 0};
      if (v.trim() && v.trim() !== '-') cols[c].filled++; else cols[c].empty++;
    }
  }
  return {hasRoot: true, title: head ? head.innerText.trim() : '', featVisible: vis(feat), rows: lists.length, cols};
}"""

# 열 N 의 제품명 / SKU (기존 JS_POP_COLUMN · JS_COLUMN 의 name / sku 와 같은 선택자)
JS_COLUMNS = r"""(rootSel) => {
  const root = document.querySelector(rootSel);
  if (!root) return [];
  const out = [];
  for (const card of root.querySelectorAll('li.pdd43-master-compare__card-item')) {
    const m = card.className.match(/__column--(\d+)/);
    const txt = s => { const e = card.querySelector(s); return e ? e.innerText.replace(/\s+/g, ' ').trim() : ''; };
    out.push({col: m ? +m[1] : null,
              name: txt('.pdd43-master-compare__name-text') || txt('.menu__select-field-text'),
              sku: txt('.pdd43-master-compare__sku-text')});
  }
  return out;
}"""

# 기존 dismiss_popups 와 같은 쿠키 배너 선택자 (거부/필수만 허용)
COOKIE_SELECTORS = ("#truste-consent-required", "#onetrust-reject-all-handler", "button[id*='reject' i]")


async def dismiss_cookie_banner(page, wait_appear_ms=0):
    """쿠키 배너를 거부(필수만)로 닫는다.

    wait_appear_ms=0 : 지금 보이는 배너만 닫음 (기다리지 않음)
    wait_appear_ms>0 : 배너가 늦게 뜨는 경우(TrustArc) 그 시간 안에 보이면 닫음 - 클릭이 가로채였을 때만 사용
    """
    if wait_appear_ms:
        try:
            await page.locator(", ".join(COOKIE_SELECTORS)).first.wait_for(state="visible", timeout=wait_appear_ms)
        except Exception as error:
            logging.debug("[COOKIE] 배너가 보이지 않음: %s", type(error).__name__)
            return
    for sel in COOKIE_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                await loc.wait_for(state="hidden", timeout=5000)
                return
        except Exception as error:   # 배너가 사라지며 클릭이 빗나가는 경우 등 - 검증과 무관
            logging.debug("[COOKIE] %s 닫기 실패: %s", sel, type(error).__name__)


def timeout_note(error) -> str:
    """Playwright 시간 초과 메시지 요약: 첫 줄 + 원인 줄(가림 요소 / 불안정 / 분리)."""
    lines = str(error).splitlines()
    cause = next((l.strip(" -") for l in lines if "intercepts pointer" in l or "not stable" in l
                  or "detached" in l), "")
    return f"대기 시간 초과: {lines[0] if lines else type(error).__name__}" + (f" ({cause[:150]})" if cause else "")


def norm(text) -> str:
    """기존 코드와 같은 공백 압축만 적용."""
    return " ".join(str(text or "").split())


async def read_state(page, root_sel=".pdd43-master-compare"):
    state = await page.evaluate(JS_STATE)
    columns = await page.evaluate(JS_COLUMNS, root_sel) if state.get("hasRoot") else []
    return state, columns


def keyspec_result(state, col, missing_section_result, missing_section_note):
    """열 col 의 Key spec 존재 결과 (기존 Landing/Popup 판정 그대로).

    섹션 미노출 처리: Landing = FAIL, Popup = 확인 필요 (세트 중 Key Specs 없는 제품이 있으면 섹션 전체가 숨는 게 규칙)
    """
    if not state.get("featVisible") or not state.get("cols"):
        return missing_section_result, missing_section_note
    c = state["cols"].get(str(col))
    if not c:
        return RESULT_FAIL, f"Key Specs 에 {col}열 값이 없음"
    detail = f"값 {c['filled']}/{c['filled'] + c['empty']}"
    return (RESULT_PASS if c["filled"] > 0 else RESULT_FAIL), detail
