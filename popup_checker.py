# -*- coding: utf-8 -*-
"""[Entry Point 2] PF POPUP Page Key Spec Checker.

공통 흐름(common/runner.py): base_urls → 국가/환경 → 로그인(p6-pre-qa3)/생략(Live) → 매핑 → Product Set
이 파일: 사업부(MX/VD/DA)마다 병렬로
  PF 열기 → Set 의 REP_MODEL 3개 Compare 체크 → Compare 버튼 → 팝업 → 제품별 Key Specs 확인

판정은 기존 run_popup_ui_live.py 와 같음
  - 섹션 노출 + 해당 열 값 있음 → PASS / 값 없음 → FAIL
  - 섹션 미노출 → 확인 필요 (세트 중 Key Specs 없는 제품이 있으면 섹션 전체가 숨는 것이 규칙)
HTTP 상태 = PF 페이지 이동(메인 문서) 응답 코드. 팝업은 페이지 이동이 아닌 레이어이므로
Compare API 응답 코드는 비고에만 따로 적는다 (같은 값으로 취급하지 않음).
Buy / 장바구니 등 데이터가 바뀌는 동작은 하지 않는다.

실행: python popup_checker.py [--dry-run] [--retries N]
"""
import dataclasses
import logging

from playwright.async_api import TimeoutError as PlaywrightTimeout

from common.config import (CATEGORY_SPECS, DIVISION_PAGES, MAX_ALTERNATES, PAGE_POPUP, POPUP_OUTPUT, RESULT_CHECK,
                           TIMEOUTS)
from common.mapping import substitution_plan
from common.keyspec_page import dismiss_cookie_banner, keyspec_result, timeout_note, read_state
from common.result import make_row, replaced_note, set_rows
from common.runner import entry

POPUP_ROOT = ".pdd43-master-compare--popup"

# 원하는 모델코드만 체크 (기존 JS_PF_SET 은 순번 기준 → 모델코드 기준으로 바꿈, DOM click 방식은 동일)
JS_SET_CHECKS = r"""(codes) => {
  const U = s => String(s || '').trim().toUpperCase(), want = codes.map(U);
  const ins = [...document.querySelectorAll('input.js-pfv2-compare-cta')];
  ins.forEach(i => { if (i.checked && !want.includes(U(i.getAttribute('data-modelcode')))) i.click(); });
  want.forEach(c => { const i = ins.find(x => U(x.getAttribute('data-modelcode')) === c); if (i && !i.checked) i.click(); });
  return ins.filter(i => i.checked).map(i => i.getAttribute('data-modelcode'));
}"""

JS_BUTTON_READY = r"""(codes) => {
  const U = s => String(s || '').trim().toUpperCase();
  const b = document.querySelector('button.js-pfv2-open-compare-layer');
  if (!b || b.disabled || b.getAttribute('disabled') !== null) return false;
  const have = (b.getAttribute('data-modelcode') || '').split(',').map(U);
  return codes.every(c => have.includes(U(c)));
}"""

JS_POPUP_READY = r"""(n) => {
  const r = document.querySelector('.pdd43-master-compare--popup.pdd43-master-compare--show');
  const l = document.getElementById('pdd43MasterCompare');
  if (!(r && l && l.getBoundingClientRect().height > 100)) return false;
  return [...r.querySelectorAll('li.pdd43-master-compare__card-item .pdd43-master-compare__sku-text')]
      .filter(e => e.innerText.trim()).length >= n;
}"""


# 새 Compare(pdd43) 가 아닌 기존 Compare 팝업/레이어가 보이는지 (클래스 이름 요약)
JS_LEGACY_POPUP = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
                     return r.width > 200 && r.height > 200 && cs.display !== 'none' && cs.visibility !== 'hidden'; };
  const els = [...document.querySelectorAll('[class*="compare" i]')]
      .filter(e => !String(e.className).includes('pdd43') && !String(e.className).includes('compare-bar') && vis(e));
  return els.length ? [...new Set(els.map(e => String(e.className).split(/\s+/)[0]))].slice(0, 3).join(', ') : '';
}"""


def pf_candidates(ps):
    """Set 카테고리(ps.page_key)가 정해져 있으면 그 PF 하나만, 아니면 사업부 PF 전체 (CATEGORY_SPECS 순서)."""
    keys = (ps.page_key,) if ps.page_key else DIVISION_PAGES[ps.division]
    return [spec for spec in CATEGORY_SPECS if spec[1] in keys]


async def find_pf(session, page, target, ps, used=()):
    """후보 PF 를 열어 Set 제품 체크박스를 찾는다. PF 에 없는 제품은 매핑 대체 후보로 바꾼다.
    반환: (PF url, HTTP, 시도 기록, 최종 제품 목록, {대체 SKU: 원래 제품})"""
    codes = [p.rep_model for p in ps.products]
    tried = []
    for _group, key, _label, pf_path, _cmp in pf_candidates(ps):
        url = f"{target.host}/{target.site}/{pf_path}"
        resp = await session.goto(page, url)
        status = resp.status if resp else None
        tried.append(f"{key}={status}")
        if not status or status >= 400:
            continue
        if not await wait_checkboxes(page):
            tried[-1] += f"(Compare 체크박스 없음 | {await pf_diag(page)})"
            continue
        found, loaded, total = await load_until_found(page, codes)
        notes = []
        if len(found) < len(codes):          # 체크박스 SKU 가 다른 옵션으로 잡힌 카드 → 옵션 전환
            found, notes = await switch_to_codes(page, codes)
        if len(found) == len(codes):
            return url, status, tried, list(ps.products), {}
        # PF 에 없는 제품 → 매핑 대체 후보를 순서대로 찾아 채움 (카드는 이미 끝까지 로드됨)
        present = set(found)
        pool = [p for p in ps.alternates if p.rep_model not in used]
        located = {i["code"]: i["how"] for i in await page.evaluate(JS_LOCATE, [p.rep_model for p in pool])} if pool else {}
        on_pf = [p for p in pool if located.get(p.rep_model, "none") != "none"]     # PF 에 있는 후보 (규칙 순서 유지)
        attempts = 0
        for cand in on_pf:
            if len(present) - len(set(found)) >= len(codes) - len(found) or attempts >= MAX_ALTERNATES:
                break
            if located[cand.rep_model] != "checkbox":
                attempts += 1                                   # 옵션 전환이 필요한 후보만 시도 횟수에 셈
            got, _ = await switch_to_codes(page, [cand.rep_model], protect=present)
            present.update(got)
        # 옵션 전환 뒤 실제로 체크박스가 남아 있는 SKU 만 사용
        present = set((await page.evaluate(JS_FOUND, sorted(present)))["found"])
        final, replaced = substitution_plan(dataclasses.replace(ps, alternates=on_pf),
                                            lambda p: p.rep_model in present, used)
        tried[-1] += f"(제품 {len(found)}/{len(codes)}, 카드 {loaded}/{total or '?'}개 로드)"
        if notes:
            tried[-1] += " [" + "; ".join(notes) + "]"
        if len(final) == len(codes):
            logging.info("[POPUP] %s %s %s PF 대체 %s", target.site, ps.division, ps.set_no,
                         {o.rep_model: s for s, o in replaced.items()})
            return url, status, tried, final, replaced
        tried[-1] += (f" [매핑 대체 후보 {len(pool)}개 중 PF 에 있는 후보 {len(on_pf)}개"
                      f"{' (' + ', '.join(p.rep_model for p in on_pf[:5]) + ')' if on_pf else ''} → 3개를 채우지 못함]")
    return None, None, tried, list(ps.products), {}


async def wait_checkboxes(page) -> bool:
    """PF 의 Compare 체크박스가 나타날 때까지 기다린다. 처음에 안 보이면 목록으로 스크롤 / View more 를 한 번 눌러
    늦게 그려지는 경우를 한 번 더 기다린다 (2026-11-09 IE·PT TV PF 에서 60초 대기 후 '체크박스 없음')."""
    box = page.locator("input.js-pfv2-compare-cta").first
    try:
        await box.wait_for(state="attached", timeout=TIMEOUTS["element"])
        return True
    except PlaywrightTimeout:
        pass
    await page.evaluate("() => { const c = document.querySelector('.pd21-product-card__item');"
                        " (c || document.body).scrollIntoView({block: 'center'});"
                        " const b = [...document.querySelectorAll('.pd21-product-finder__view-more')].find(e => e.offsetParent);"
                        " if (b) b.click(); }")
    try:
        await box.wait_for(state="attached", timeout=TIMEOUTS["lazy_load"])
        return True
    except PlaywrightTimeout:
        return False


JS_PF_DIAG = r"""() => {
  const cards = document.querySelectorAll('.pd21-product-card__item');
  const inputs = [...document.querySelectorAll('.pd21-product-card__item input')].map(i => String(i.className).split(/\s+/)[0] || i.type);
  const cmp = [...new Set([...document.querySelectorAll('.pd21-product-card__item [class*="compare" i]')]
      .map(e => String(e.className).split(/\s+/).find(c => /compare/i.test(c))).filter(Boolean))].slice(0, 6);
  // 카드 안 Compare 영역 (TV PF 처럼 input 이 없을 때 실제 구조 확인용)
  const first = [...cards].find(c => c.querySelector('[class*="compare" i]'));
  const area = first && first.querySelector('[class*="compare" i]');
  const snippet = area ? area.outerHTML.replace(/\s+/g, ' ').slice(0, 260) : '';
  const withCompare = [...cards].filter(c => c.querySelector('[class*="compare" i]')).length;
  return {url: location.pathname, title: document.title.slice(0, 50), cards: cards.length,
          inputs: [...new Set(inputs)].slice(0, 4), compare: cmp, withCompare, snippet,
          viewMore: !!document.querySelector('.pd21-product-finder__view-more')};
}"""


async def pf_diag(page) -> str:
    try:
        d = await page.evaluate(JS_PF_DIAG)
        total = await page.evaluate(JS_RESULT_TOTAL)
    except Exception as error:
        return f"진단 실패 {type(error).__name__}"
    return (f"진단: 최종 경로 {d['url']}, 제목 '{d['title']}', 카드 {d['cards']}개 / 결과 수 {total or '?'}, "
            f"카드 안 input {d['inputs']}, Compare 영역 있는 카드 {d['withCompare']}개, "
            f"View more {'O' if d['viewMore'] else 'X'}, 카드 Compare 영역 HTML: {d['snippet'] or '없음'}")


# 카드 단위 탐색 (2026-11-09: ae Galaxy Z Fold8 / uk Galaxy Tab S11 Ultra (14.6, Wi-Fi) 미발견 건)
# PF 카드 1장 = 제품 1개 + 옵션(색상 option-selector-v2__color / 용량·사이즈 option-selector-v2__size).
# 카드의 체크박스·이미지 링크·제품명 링크의 data-modelcode 는 '지금 선택된 옵션' 의 SKU 뿐이다.
#   - 매핑 SKU 가 다른 옵션 버튼의 data-modelcode 에 있으면 → 그 버튼을 누르면 됨 (1단계)
#   - 색상과 용량을 둘 다 바꿔야 하는 SKU (예: SM-X930NZAREUB = Grey + 256GB 인데 카드 기본이 Silver)
#     는 DOM 어디에도 SKU 가 없다 → data-modelname(예: "SM-X930") 으로 카드를 찾고 색상 버튼을 하나씩 눌러 본다 (2단계)
# SKU 비교는 대소문자 무시 (링크 href 에는 소문자 sku 가 들어 있음).

_JS_HELPERS = r"""
  const U = s => String(s || '').trim().toUpperCase();
  const cards = () => [...document.querySelectorAll('.pd21-product-card__item')];
  const attrsHave = (root, code) => [root, ...root.querySelectorAll('*')].some(e =>
      [...e.attributes].some(a => a.name.startsWith('data-') && U(a.value) === code));
  const boxOf = card => card.querySelector('input.js-pfv2-compare-cta');
  const isNav = e => { const a = e.closest('a[href]'); const h = a ? a.getAttribute('href') || '' : '';
                       return !!a && !!h && !h.startsWith('#') && !h.startsWith('javascript'); };
  const optionButtons = card => [...card.querySelectorAll('[class*="option-selector"] button, button[class*="option-selector"], [class*="option-selector"] input[type="radio"]')]
      .filter(e => !isNav(e) && !e.matches('input.js-pfv2-compare-cta'));
"""

# 체크박스 data-modelcode 로 찾은 SKU + 로드된 카드 수
JS_FOUND = r"""(codes) => {""" + _JS_HELPERS + r"""
  const boxes = [...document.querySelectorAll('input.js-pfv2-compare-cta')].map(i => U(i.getAttribute('data-modelcode')));
  return {found: codes.filter(c => boxes.includes(U(c))), count: boxes.length, cards: cards().length};
}"""

# PF 상단 'N Results' (전체 제품 수). 못 읽으면 null
JS_RESULT_TOTAL = r"""() => {
  // 영어 'N Results' 외 현지어(예: '24 találat', '32 resultat', '119 Resultados')도 읽도록
  // PF 안에서 class 에 result/count/total 이 들어간 짧은 텍스트의 첫 숫자를 우선 사용
  const num = t => { const m = (t || '').match(/(\d[\d.,\s]*)/); return m ? parseInt(m[1].replace(/[^\d]/g, ''), 10) : null; };
  const inPf = e => e.closest('[class*="product-finder" i], [class*="pd21" i]') && !e.closest('.pd21-product-card__item')
                   && e.children.length <= 3;   // 카드 안 별점 개수 등은 제외
  const els = [...document.querySelectorAll('[class*="result" i]')].filter(inPf)
      .concat([...document.querySelectorAll('[class*="count" i], [class*="total" i]')].filter(inPf));
  for (const e of els) { const t = (e.textContent || '').trim(); if (t && t.length <= 40) { const n = num(t); if (n) return n; } }
  const m = (document.body.innerText || '').match(/(\d[\d,]*)\s+Results?\b/i);
  return m ? parseInt(m[1].replace(/,/g, ''), 10) : null;
}"""

JS_LOCATE = r"""(codes) => {""" + _JS_HELPERS + r"""
  const all = cards();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  return codes.map(code0 => {
    const code = U(code0);
    const boxes = [...document.querySelectorAll('input.js-pfv2-compare-cta')];
    if (boxes.some(i => U(i.getAttribute('data-modelcode')) === code)) return {code: code0, how: 'checkbox'};
    let idx = all.findIndex(c => attrsHave(c, code)), how = 'option';
    if (idx < 0) {          // 2단계: data-modelname(모델 기본형, 예 SM-X930) 이 SKU 앞부분과 같은 카드
      idx = all.findIndex(c => [...c.querySelectorAll('[data-modelname]')].some(e => {
        const m = U(e.getAttribute('data-modelname')); return m.length >= 5 && code.startsWith(m); }));
      how = 'family';
    }
    if (idx < 0) return {code: code0, how: 'none',
                         inPage: (document.documentElement.outerHTML || '').toUpperCase().includes(code)};
    const card = all[idx], box = boxOf(card);
    const nameEl = card.querySelector('.pd21-product-card__name, [class*="product-card__name"]');
    return {code: code0, how, card: idx, name: clean(nameEl && nameEl.innerText).slice(0, 60),
            checkbox: box ? box.getAttribute('data-modelcode') : null,
            options: optionButtons(card).length};
  });
}"""

# 카드 안에서 data-* 값이 code 인 옵션 버튼을 누른다 (페이지 이동 링크·Buy·Compare 체크박스 제외)
JS_CLICK_CODE_OPTION = r"""([cardIdx, code0]) => {""" + _JS_HELPERS + r"""
  const card = cards()[cardIdx], code = U(code0);
  if (!card) return 'no-card';
  const hit = optionButtons(card).find(e => [...e.attributes].some(a => a.name.startsWith('data-') && U(a.value) === code))
           || [...card.querySelectorAll('*')].find(e => !isNav(e) && !e.matches('input.js-pfv2-compare-cta') &&
                !/buy|cart|learn|quick/i.test(String(e.className || '')) &&
                [...e.attributes].some(a => a.name.startsWith('data-') && U(a.value) === code) &&
                e.matches('input, button, [role="radio"], [role="option"], [class*="option" i]'));
  if (!hit) return 'no-option';
  hit.click();
  return 'clicked:' + hit.tagName.toLowerCase() + '.' + String(hit.className || '').trim().split(/\s+/)[0];
}"""

# 카드의 옵션 버튼 n 번째를 누른다 (색상 버튼 먼저 정렬). 반환: 누른 버튼 설명 또는 null
JS_CLICK_NTH_OPTION = r"""([cardIdx, n]) => {""" + _JS_HELPERS + r"""
  const card = cards()[cardIdx];
  if (!card) return null;
  const opts = optionButtons(card).sort((a, b) => (/color/i.test(b.className) ? 1 : 0) - (/color/i.test(a.className) ? 1 : 0));
  const o = opts[n];
  if (!o) return null;
  o.click();
  return String(o.className || '').trim().split(/\s+/)[0] + ':' + (o.getAttribute('data-modelcode') || o.innerText || '').trim();
}"""

JS_CARD_HAS = r"""([cardIdx, code0]) => {""" + _JS_HELPERS + r"""
  const card = cards()[cardIdx]; return !!card && attrsHave(card, U(code0));
}"""

JS_BOX_IS = r"""(code0) => {""" + _JS_HELPERS + r"""
  return [...document.querySelectorAll('input.js-pfv2-compare-cta')].some(i => U(i.getAttribute('data-modelcode')) === U(code0));
}"""


def locate_note(info) -> str:
    """미발견 SKU 진단 문자열 (비고 기록용)."""
    if info["how"] == "none":
        return f"{info['code']}: PF 카드에 없음 (모델 기본형 일치 카드도 없음, 페이지 HTML 포함 {'O' if info.get('inPage') else 'X'})"
    kind = "옵션 버튼에 SKU 있음" if info["how"] == "option" else "모델 기본형 일치"
    return (f"{info['code']}: {info['card'] + 1}번째 카드 '{info.get('name', '')}' ({kind}, "
            f"체크박스 SKU {info.get('checkbox')}, 옵션 버튼 {info.get('options')}개)")


async def _wait_box(page, code, timeout=5000) -> bool:
    try:
        await page.wait_for_function(JS_BOX_IS, arg=code, timeout=timeout)
        return True
    except PlaywrightTimeout:
        return False


async def _switch_family(page, card_idx, code, max_options=12):
    """색상(먼저)·용량 버튼을 하나씩 눌러 보며 카드에 code 옵션이 나타나는지 확인 → 나타나면 그 옵션을 누른다."""
    for n in range(max_options):
        clicked = await page.evaluate(JS_CLICK_NTH_OPTION, [card_idx, n])
        if clicked is None:
            return False, f"옵션 {n}개 시도"
        try:   # 옵션을 누르면 카드의 data-modelcode 들이 다시 그려짐 → code 가 나타나는지 짧게 확인
            await page.wait_for_function(JS_CARD_HAS, arg=[card_idx, code], timeout=1500)
        except PlaywrightTimeout:
            continue
        if await _wait_box(page, code, 1000):
            return True, clicked
        action = await page.evaluate(JS_CLICK_CODE_OPTION, [card_idx, code])
        if await _wait_box(page, code):
            return True, f"{clicked} → {action}"
    return False, f"옵션 {max_options}개 시도"


async def switch_to_codes(page, codes, protect=()):
    """체크박스로 못 찾은 SKU 를 카드 옵션 전환으로 찾는다. 반환: (찾은 SKU 목록, 진단 문자열 목록).
    protect: 이미 확보한 SKU - 그 SKU 가 체크박스에 걸린 카드는 옵션을 바꾸지 않는다."""
    notes = []
    protect_u = {str(c).upper() for c in protect}
    for info in await page.evaluate(JS_LOCATE, codes):
        if info["how"] in ("checkbox", "none"):
            if info["how"] == "none":
                notes.append(locate_note(info))
            continue
        if str(info.get("checkbox") or "").upper() in protect_u:
            notes.append(locate_note(info) + " → 이미 고른 제품의 카드라 옵션 전환 안 함")
            continue
        if info["how"] == "option":
            action = await page.evaluate(JS_CLICK_CODE_OPTION, [info["card"], info["code"]])
            ok = await _wait_box(page, info["code"])
        else:
            ok, action = await _switch_family(page, info["card"], info["code"])
        if ok:
            logging.info("[POPUP] PF 카드 옵션 전환으로 %s 체크박스 확보 (%s)", info["code"], action)
        else:
            notes.append(locate_note(info) + f" → 옵션 전환 실패({action})")
    found = (await page.evaluate(JS_FOUND, codes))["found"]
    return found, notes


async def _view_more_ready(view_more) -> bool:
    """'View more' 가 보이면 True. 안 보이면 lazy_load 시간 동안 다시 나타나는지 기다린다."""
    if await view_more.count():
        return True
    try:
        await view_more.wait_for(state="visible", timeout=TIMEOUTS["lazy_load"])
        return True
    except PlaywrightTimeout:
        return False


async def load_until_found(page, codes, max_rounds=60):
    """PF 첫 화면은 카드 일부만 있고, 목록 하단 'View more'(.pd21-product-finder__view-more)로 더 불러온다.
    대상 SKU 를 모두 찾거나, 'N Results' 만큼 카드가 다 나오거나, 'View more' 가 끝날 때까지 반복한다.
    카드 수는 체크박스가 아니라 카드(.pd21-product-card__item) 기준 (체크박스 없는 카드가 있어도 멈추지 않도록).
    반환: (찾은 SKU, 로드된 카드 수, 'N Results' 값)"""
    total = await page.evaluate(JS_RESULT_TOTAL)
    state = await page.evaluate(JS_FOUND, codes)
    if total and total < state["cards"]:      # 결과 수를 잘못 읽은 경우 (이미 보이는 카드보다 적음) → 사용 안 함
        total = None
    view_more = page.locator(".pd21-product-finder__view-more").locator("visible=true").first
    stalls = 0
    for _ in range(max_rounds):
        if len(state["found"]) == len(codes):
            break
        if total and state["cards"] >= total:
            break
        prev = state["cards"]
        await page.evaluate("() => { const c = [...document.querySelectorAll('.pd21-product-card__item')];"
                            " (c[c.length - 1] || document.body).scrollIntoView({block: 'center'}); }")
        # 'View more' 는 다음 묶음을 불러오는 동안 잠시 숨는다 → 일정 시간 안에 다시 보이지 않을 때만 '끝'
        if not await _view_more_ready(view_more):
            break
        # DOM click: 쿠키 배너·다시 그려지는 카드 목록이 버튼을 가려 화면 클릭이 막히는 경우가 있음
        clicked = await page.evaluate(
            "() => { const b = [...document.querySelectorAll('.pd21-product-finder__view-more')]"
            ".find(e => e.offsetParent); if (!b) return false; b.click(); return true; }")
        if not clicked:
            continue                    # 그 사이 숨겨짐 → 다음 반복에서 다시 확인
        try:
            await page.wait_for_function(
                "(prev) => document.querySelectorAll('.pd21-product-card__item').length > prev",
                arg=prev, timeout=TIMEOUTS["lazy_load"])
            stalls = 0
        except PlaywrightTimeout:
            stalls += 1
            if stalls >= 3 or not await _view_more_ready(view_more):
                break                   # 카드가 늘지 않고 버튼도 없음(또는 3번 연속 무반응) = 끝
        state = await page.evaluate(JS_FOUND, codes)
    state = await page.evaluate(JS_FOUND, codes)
    return state["found"], state["cards"], total


async def check_set(session, ctx, target, ps, used=()):
    page = await ctx.new_page()
    try:
        pf_url, status, tried, final, replaced = await find_pf(session, page, target, ps, used)
        if not pf_url:
            return set_rows(target, PAGE_POPUP, ps, RESULT_CHECK,
                            "PF 에서 3개 제품 체크박스를 모두 찾지 못함 (시도: " + ", ".join(tried) + ")")
        ps = dataclasses.replace(ps, products=final)      # 대체 반영 (원래 Set 객체는 그대로)
        codes = [p.rep_model for p in ps.products]
        await dismiss_cookie_banner(page)
        checked = await page.evaluate(JS_SET_CHECKS, codes)
        await page.wait_for_function(JS_BUTTON_READY, arg=codes, timeout=TIMEOUTS["element"])

        # Compare 버튼 → 팝업. 판정은 'API 주소' 가 아니라 '새 Compare 팝업이 실제로 뜨는지' 로 한다.
        # (2026-11-09 IE 스마트폰/냉장고: 첫 응답이 기존 spec/compare 라서 새 팝업을 기다리지 않고 '미배포' 로 오판)
        apis = []

        def on_response(r):
            if "/compare" in r.url and r.request.resource_type in ("xhr", "fetch"):
                apis.append(f"{r.status} {r.url.split('?')[0].rsplit('/product/', 1)[-1]}")
        page.on("response", on_response)
        try:
            await page.evaluate("() => document.querySelector('button.js-pfv2-open-compare-layer').click()")
            try:
                await page.wait_for_function(JS_POPUP_READY, arg=len(codes), timeout=TIMEOUTS["popup"])
                new_popup = True
            except PlaywrightTimeout:
                new_popup = False
        finally:
            page.remove_listener("response", on_response)
        api_note = f"Compare API {', '.join(apis[:3])}" if apis else "Compare API 응답 없음"
        if not new_popup:
            legacy = await page.evaluate(JS_LEGACY_POPUP)
            what = (f"기존 Compare 팝업이 열림 ({legacy})" if legacy else "Compare 팝업이 열리지 않음")
            logging.warning("[POPUP] %s %s %s 새 Compare 팝업 없음 - %s | %s", target.site, ps.division, ps.set_no,
                            what, api_note)
            rows = []
            for p in ps.products:     # 제품은 PF 에서 찾아 체크까지 했음 → 비고에 분명히 남김
                rows.append(make_row(target, PAGE_POPUP, ps, p, http=status, result=RESULT_CHECK,
                                     note=replaced_note(p, replaced) + f"PF 에서 제품 찾음·체크 완료, 그러나 새 Compare 팝업"
                                     f"(pdd43-master-compare) 이 뜨지 않아 Key spec 판정 불가 - {what} | {api_note} | PF {pf_url}"))
            return rows

        state, columns = await read_state(page, POPUP_ROOT)
        by_sku = {c["sku"]: c for c in columns if c["sku"]}
        rows = []
        for p in ps.products:
            col = by_sku.get(p.rep_model)
            if not col:
                rows.append(make_row(target, PAGE_POPUP, ps, p, final_sku=", ".join(by_sku) or "-", http=status, result=RESULT_CHECK,
                                     note=replaced_note(p, replaced) + f"팝업에서 SKU {p.rep_model} 를 찾지 못함 (팝업 SKU: {list(by_sku)}) | {api_note}"))
                continue
            result, detail = keyspec_result(
                state, col["col"], RESULT_CHECK,
                "Key Specs 섹션 미노출 - 세트 중 Key Specs 없는 제품이 있으면 섹션 전체 미노출이 규칙")
            rows.append(make_row(target, PAGE_POPUP, ps, p, final_text=col["name"], final_sku=col["sku"], http=status, result=result,
                                 note=replaced_note(p, replaced) + f"{col['col']}열 {detail} | {api_note} | PF {pf_url}"))
        logging.info("[POPUP] %s %s %s 체크 %s → %s", target.site, ps.division, ps.set_no, checked,
                     [r["Key spec 존재 결과"] for r in rows])
        return rows
    finally:
        await page.close()


async def check_division(session, target, division, sets):
    """사업부 워커: Set 1 → Set 2 순서로 처리하고 행만 돌려준다 (Excel 은 쓰지 않음)."""
    rows, used = [], set()       # used: 앞 Set 에서 쓴 SKU (대체 후보 중복 방지)
    async with session.worker_context() as ctx:
        for ps in sets:
            try:
                set_result = await check_set(session, ctx, target, ps, used)
            except PlaywrightTimeout as error:   # 화면 조건 미충족 → 이 Set 만 '확인 필요', 다음 Set 계속
                set_result = set_rows(target, PAGE_POPUP, ps, RESULT_CHECK, timeout_note(error))
            used.update(r["SKU"] for r in set_result if r.get("SKU"))
            rows += set_result
    return rows


if __name__ == "__main__":
    entry(PAGE_POPUP, check_division, POPUP_OUTPUT, "PF POPUP Page Key Spec Checker")
