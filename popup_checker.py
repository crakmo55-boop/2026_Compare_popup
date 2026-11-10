# -*- coding: utf-8 -*-
"""[Entry Point 2] PF POPUP Page Key Spec Checker.

공통 흐름(common/runner.py): base_urls → 국가/환경 → 로그인(p6-pre-qa3)/생략(Live) → 매핑 → Product Set
이 파일: 사업부(MX/VD/DA)마다 병렬로, Set 마다 새 Context 에서
  PF 열기 → Set 의 REP_MODEL 3개 Compare 체크 → Compare 버튼 → 팝업 → 제품별 Key Specs 확인
  → Key Specs 가 없으면(열 값 없음 / 섹션 미노출 / 새 팝업 미표시) 같은 사업부·같은 카테고리의 다른 매핑 제품으로
    바꿔 다시 확인 (common/substitute.py, 2026-11-10 사용자 지시)

판정은 기존 run_popup_ui_live.py 와 같음
  - 섹션 노출 + 해당 열 값 있음 → PASS / 값 없음 → FAIL
  - 섹션 미노출 → 확인 필요 (세트 중 Key Specs 없는 제품이 있으면 섹션 전체가 숨는 것이 규칙)
HTTP 상태 = PF 페이지 이동(메인 문서) 응답 코드. 팝업은 페이지 이동이 아닌 레이어이므로
Compare API 응답 코드는 비고에만 따로 적는다 (같은 값으로 취급하지 않음).
Buy / 장바구니 등 데이터가 바뀌는 동작은 하지 않는다.

실행: python popup_checker.py [--dry-run] [--retries N]
"""
import asyncio
import dataclasses
import logging
import time

from playwright.async_api import TimeoutError as PlaywrightTimeout

from common.config import (CATEGORY_SPECS, DIVISION_PAGES, KEYSPEC_SUBSTITUTE_ROUNDS, MAX_ALTERNATES, PAGE_POPUP,
                           POPUP_OUTPUT, POPUP_SET_BUDGET_S, RESULT_CHECK, SET_ATTEMPTS, TIMEOUTS)
from common.keyspec_page import (column_verdict, dismiss_cookie_banner, ev, keyspec_result, read_state, snap,
                                 timeout_note, wf)
from common.mapping import substitution_plan
from common.result import make_row, replaced_note, set_rows
from common.runner import entry
from common.substitute import V_BUTTON, V_NOPOPUP, V_NOSKU, KeySpecRetry

POPUP_ROOT = ".pdd43-master-compare--popup"          # read_state 는 이 중 '화면에 보이는' 루트를 읽는다
POPUP_SHOWN = ".pdd43-master-compare--popup.pdd43-master-compare--show"
POPUP_CLOSE = "#pdd43MasterCompare button.layer-popup__close"   # 기존 run_popup_ui_live.check_close 와 같은 버튼

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

# Compare 버튼이 준비되지 않을 때의 진단 (2026-11-10 CA DA Set 1: 60초 대기 후 원인 없이 '대기 시간 초과')
JS_BUTTON_DIAG = r"""(codes) => {
  const U = s => String(s || '').trim().toUpperCase();
  const b = document.querySelector('button.js-pfv2-open-compare-layer');
  const ins = [...document.querySelectorAll('input.js-pfv2-compare-cta')];
  const vis = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const msgs = [...document.querySelectorAll('[class*="toast" i], [class*="alert" i], [role="alert"]')].filter(vis)
      .map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 2);
  return {button: b ? (b.getAttribute('data-modelcode') || '') : null, disabled: b ? (b.disabled || b.getAttribute('disabled') !== null) : null,
          checked: ins.filter(i => i.checked).map(i => i.getAttribute('data-modelcode')),
          boxes: codes.map(c => ins.some(i => U(i.getAttribute('data-modelcode')) === U(c))), msgs};
}"""

JS_POPUP_READY = r"""(n) => {
  const r = document.querySelector('.pdd43-master-compare--popup.pdd43-master-compare--show');
  const l = document.getElementById('pdd43MasterCompare');
  if (!(r && l && l.getBoundingClientRect().height > 100)) return false;
  return [...r.querySelectorAll('li.pdd43-master-compare__card-item .pdd43-master-compare__sku-text')]
      .filter(e => (e.innerText || e.textContent || '').trim()).length >= n;
}"""

JS_POPUP_SHOWN = "() => !!document.querySelector('.pdd43-master-compare--popup.pdd43-master-compare--show')"

JS_FEATURE_DRAWN = r"""() => { const r = document.querySelector('.pdd43-master-compare--popup.pdd43-master-compare--show');
  const f = r && r.querySelector('.pdd43-master-compare__feature');
  return !!(f && f.offsetParent !== null && f.querySelector('.pdd43-master-compare__feature-item')); }"""


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


def _tier(info) -> int:
    """PF 에서 후보를 쓰기 쉬운 순서 (2026-11-10 LV/EE/LATIN: 옵션 전환 실패 후보 10개에 막혀 체크박스가 바로 있는
    후보를 시도하지 못함 → 체크박스 있는 후보를 먼저).
      0 = 체크박스 바로 있음 / 1 = 옵션 전환 필요, 카드에 Compare 체크박스 있음
      2 = 옵션 전환 필요, 카드에 체크박스 없음(Compare 미제공 카드일 수 있음) / 9 = PF 에 없음"""
    how = (info or {}).get("how", "none")
    if how == "checkbox":
        return 0
    if how in ("option", "family"):
        return 1 if info.get("checkbox") else 2
    return 9


async def find_pf(session, page, target, ps, used=()):
    """후보 PF 를 열어 Set 제품 체크박스를 찾는다. PF 에 없는 제품은 매핑 대체 후보로 바꾼다.
    반환: (PF url, HTTP, 시도 기록, 최종 제품 목록, {대체 SKU: (원래 제품, 사유)}, Key spec 대체용 후보 목록)"""
    codes = [p.rep_model for p in ps.products]
    tried = []
    for _group, key, _label, pf_path, _cmp in pf_candidates(ps):
        url = f"{target.host}/{target.site}/{pf_path}"
        resp = await session.goto(page, url)
        status = resp.status if resp else None
        tried.append(f"{key}={status}")
        if not status or status >= 400:
            continue
        box_state = await wait_checkboxes(page)
        if box_state == "no-data":
            tried[-1] += ("(사이트 PF 카드에 Compare 기능 없음 - 카드 Compare 영역이 모두 "
                          f"'pd21-product-card__compare--no-data' | {await pf_diag(page)})")
            continue
        if not box_state:
            tried[-1] += f"(Compare 체크박스 없음 | {await pf_diag(page)})"
            continue
        found, loaded, total, complete = await load_until_found(page, codes)
        notes = []
        if len(found) < len(codes):          # 체크박스 SKU 가 다른 옵션으로 잡힌 카드 → 옵션 전환
            found, notes = await switch_to_codes(page, codes)

        pool = [p for p in ps.alternates if p.rep_model not in used and p.rep_model not in codes]
        located = ({i["code"]: i for i in await ev(page, JS_LOCATE, [p.rep_model for p in pool])} if pool else {})
        on_pf = sorted((p for p in pool if _tier(located.get(p.rep_model)) < 9),
                       key=lambda p: _tier(located.get(p.rep_model)))           # 정렬은 안정적 → 같은 단계는 규칙 순서
        later = [] if complete else [p for p in pool if _tier(located.get(p.rep_model)) == 9]   # 아직 안 불러온 카드에 있을 수 있음

        if len(found) == len(codes):
            return url, status, tried, list(ps.products), {}, on_pf + later

        # PF 에 없는 제품 → 매핑 대체 후보를 체크박스 있는 것부터 채움
        present, option_tries = set(found), 0
        need = len(codes) - len(found)
        for cand in on_pf:
            if len(present - set(found)) >= need:
                break
            t = _tier(located.get(cand.rep_model))
            if t == 0:
                present.add(cand.rep_model)
                continue
            if option_tries >= MAX_ALTERNATES:
                continue
            option_tries += 1
            got, _ = await switch_to_codes(page, [cand.rep_model], protect=present)
            present.update(got)
        # 옵션 전환 뒤 실제로 체크박스가 남아 있는 SKU 만 사용
        present = set((await ev(page, JS_FOUND, sorted(present)))["found"])
        final, replaced = substitution_plan(dataclasses.replace(ps, alternates=on_pf),
                                            lambda p: p.rep_model in present, used)
        tried[-1] += f"(제품 {len(found)}/{len(codes)}, 카드 {loaded}/{total or '?'}개 로드)"
        if notes:
            tried[-1] += " [" + "; ".join(notes) + "]"
        if len(final) == len(codes):
            logging.info("[POPUP] %s %s %s PF 대체 %s", target.site, ps.division, ps.set_no,
                         {o.rep_model: s for s, o in replaced.items()})
            rest = [p for p in on_pf + later if p.rep_model not in {f.rep_model for f in final}]
            why = "PF 에 없음 (View more 로 목록 끝까지 확인)" if complete else "PF 에서 체크박스를 찾지 못함"
            return url, status, tried, final, {s: (o, why) for s, o in replaced.items()}, rest
        tiers = {}
        for p in pool:
            tiers[_tier(located.get(p.rep_model))] = tiers.get(_tier(located.get(p.rep_model)), 0) + 1
        tried[-1] += (f" [매핑 대체 후보 {len(pool)}개: 체크박스 바로 있음 {tiers.get(0, 0)}, 옵션 전환 필요 "
                      f"{tiers.get(1, 0) + tiers.get(2, 0)}(그중 카드에 체크박스 없음 {tiers.get(2, 0)}), PF 에 없음 {tiers.get(9, 0)}"
                      f"{'' if complete else ' (목록을 끝까지 불러오지 못함)'} → 3개를 채우지 못함]")
    return None, None, tried, list(ps.products), {}, []


JS_PF_BOXSTATE = r"""() => {
  const cards = [...document.querySelectorAll('.pd21-product-card__item')];
  const areas = cards.filter(c => c.querySelector('.pd21-product-card__compare'));
  return {cards: cards.length, boxes: document.querySelectorAll('input.js-pfv2-compare-cta').length, areas: areas.length,
          nodata: areas.filter(c => c.querySelector('.pd21-product-card__compare--no-data')).length};
}"""


async def wait_checkboxes(page):
    """PF 의 Compare 체크박스가 나타날 때까지 기다린다. 반환: True / 'no-data' / False.
    2026-11-10: IE·PT·BG·LT TV PF 에서 카드 Compare 영역이 그려지기 전에 판단해 'no-data' 대신 '체크박스 없음' 으로
    60초+ 대기한 경우가 있었음 → '체크박스 있음' 또는 '카드 Compare 영역이 전부 no-data' 중 하나가 될 때까지 기다린다."""
    cond = ("() => { const s = (" + JS_PF_BOXSTATE + ")(); "
            "return s.boxes > 0 || (s.areas > 0 && s.nodata === s.areas); }")
    try:
        await wf(page, cond, timeout=TIMEOUTS["element"])
    except PlaywrightTimeout:
        pass
    s = await ev(page, JS_PF_BOXSTATE)
    if s["boxes"]:
        return True
    if s["areas"] and s["nodata"] == s["areas"]:
        return "no-data"
    # 카드가 늦게 그려지는 경우: 목록으로 스크롤 + View more 한 번 → 짧게 다시 확인
    await ev(page, "() => { const c = document.querySelector('.pd21-product-card__item');"
                   " (c || document.body).scrollIntoView({block: 'center'});"
                   " const b = [...document.querySelectorAll('.pd21-product-finder__view-more')].find(e => e.offsetParent);"
                   " if (b) b.click(); }")
    try:
        await wf(page, cond, timeout=TIMEOUTS["lazy_load"])
    except PlaywrightTimeout:
        pass
    s = await ev(page, JS_PF_BOXSTATE)
    if s["boxes"]:
        return True
    return "no-data" if s["areas"] and s["nodata"] == s["areas"] else False


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
        d = await ev(page, JS_PF_DIAG)
        total = await ev(page, JS_RESULT_TOTAL)
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
      .filter(e => !isNav(e) && !e.matches('input.js-pfv2-compare-cta') && !/tooltip/i.test(String(e.className || '')));
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
            nodata: !!card.querySelector('.pd21-product-card__compare--no-data'),
            options: optionButtons(card).length};
  });
}"""

# 카드 안에서 data-* 값이 code 인 옵션 버튼을 누른다 (페이지 이동 링크·Buy·Compare 체크박스·툴팁 제외)
JS_CLICK_CODE_OPTION = r"""([cardIdx, code0]) => {""" + _JS_HELPERS + r"""
  const card = cards()[cardIdx], code = U(code0);
  if (!card) return 'no-card';
  const hit = optionButtons(card).find(e => [...e.attributes].some(a => a.name.startsWith('data-') && U(a.value) === code))
           || [...card.querySelectorAll('[class*="option" i] *, [class*="option" i]')].find(e => !isNav(e)
                && !e.matches('input.js-pfv2-compare-cta') && !/buy|cart|learn|quick|tooltip/i.test(String(e.className || '')) &&
                [...e.attributes].some(a => a.name.startsWith('data-') && U(a.value) === code) &&
                e.matches('input, button, [role="radio"], [role="option"]'));
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
    box = info.get("checkbox") or ("없음 - 카드 Compare 미제공(no-data)" if info.get("nodata") else "없음")
    return (f"{info['code']}: {info['card'] + 1}번째 카드 '{info.get('name', '')}' ({kind}, "
            f"체크박스 SKU {box}, 옵션 버튼 {info.get('options')}개)")


async def _wait_box(page, code, timeout=5000) -> bool:
    try:
        await wf(page, JS_BOX_IS, code, timeout=timeout)
        return True
    except PlaywrightTimeout:
        return False


async def _switch_family(page, card_idx, code, max_options=12):
    """색상(먼저)·용량 버튼을 하나씩 눌러 보며 카드에 code 옵션이 나타나는지 확인 → 나타나면 그 옵션을 누른다."""
    for n in range(max_options):
        clicked = await ev(page, JS_CLICK_NTH_OPTION, [card_idx, n])
        if clicked is None:
            return False, f"옵션 {n}개 시도"
        try:   # 옵션을 누르면 카드의 data-modelcode 들이 다시 그려짐 → code 가 나타나는지 짧게 확인
            await wf(page, JS_CARD_HAS, [card_idx, code], timeout=1500)
        except PlaywrightTimeout:
            continue
        if await _wait_box(page, code, 1000):
            return True, clicked
        action = await ev(page, JS_CLICK_CODE_OPTION, [card_idx, code])
        if await _wait_box(page, code):
            return True, f"{clicked} → {action}"
    return False, f"옵션 {max_options}개 시도"


async def switch_to_codes(page, codes, protect=()):
    """체크박스로 못 찾은 SKU 를 카드 옵션 전환으로 찾는다. 반환: (찾은 SKU 목록, 진단 문자열 목록).
    protect: 이미 확보한 SKU - 그 SKU 가 체크박스에 걸린 카드는 옵션을 바꾸지 않는다."""
    notes = []
    protect_u = {str(c).upper() for c in protect}
    for info in await ev(page, JS_LOCATE, codes):
        if info["how"] in ("checkbox", "none"):
            if info["how"] == "none":
                notes.append(locate_note(info))
            continue
        if str(info.get("checkbox") or "").upper() in protect_u:
            notes.append(locate_note(info) + " → 이미 고른 제품의 카드라 옵션 전환 안 함")
            continue
        if info["how"] == "option":
            action = await ev(page, JS_CLICK_CODE_OPTION, [info["card"], info["code"]])
            ok = await _wait_box(page, info["code"])
        else:
            ok, action = await _switch_family(page, info["card"], info["code"])
        if ok:
            logging.info("[POPUP] PF 카드 옵션 전환으로 %s 체크박스 확보 (%s)", info["code"], action)
        else:
            notes.append(locate_note(info) + f" → 옵션 전환 실패({action})")
    found = (await ev(page, JS_FOUND, codes))["found"]
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
    반환: (찾은 SKU, 로드된 카드 수, 'N Results' 값, 목록 끝까지 불러왔는지)"""
    total = await ev(page, JS_RESULT_TOTAL)
    state = await ev(page, JS_FOUND, codes)
    if total and total < state["cards"]:      # 결과 수를 잘못 읽은 경우 (이미 보이는 카드보다 적음) → 사용 안 함
        total = None
    view_more = page.locator(".pd21-product-finder__view-more").locator("visible=true").first
    stalls, complete = 0, False
    for _ in range(max_rounds):
        if len(state["found"]) == len(codes):
            break
        if total and state["cards"] >= total:
            complete = True
            break
        prev = state["cards"]
        await ev(page, "() => { const c = [...document.querySelectorAll('.pd21-product-card__item')];"
                       " (c[c.length - 1] || document.body).scrollIntoView({block: 'center'}); }")
        # 'View more' 는 다음 묶음을 불러오는 동안 잠시 숨는다 → 일정 시간 안에 다시 보이지 않을 때만 '끝'
        if not await _view_more_ready(view_more):
            complete = True
            break
        # DOM click: 쿠키 배너·다시 그려지는 카드 목록이 버튼을 가려 화면 클릭이 막히는 경우가 있음
        clicked = await ev(page,
                           "() => { const b = [...document.querySelectorAll('.pd21-product-finder__view-more')]"
                           ".find(e => e.offsetParent); if (!b) return false; b.click(); return true; }")
        if not clicked:
            continue                    # 그 사이 숨겨짐 → 다음 반복에서 다시 확인
        try:
            await wf(page, "(prev) => document.querySelectorAll('.pd21-product-card__item').length > prev",
                     prev, timeout=TIMEOUTS["lazy_load"])
            stalls = 0
        except PlaywrightTimeout:
            stalls += 1
            if stalls >= 3 or not await _view_more_ready(view_more):
                complete = True
                break                   # 카드가 늘지 않고 버튼도 없음(또는 3번 연속 무반응) = 끝
        state = await ev(page, JS_FOUND, codes)
    state = await ev(page, JS_FOUND, codes)
    return state["found"], state["cards"], total, complete


# ------------------------------------------------------------------ 팝업 열기 / 닫기
async def close_popup(page) -> bool:
    """떠 있는 새 Compare 팝업을 닫는다 (기존 check_close 와 같은 X 버튼 → 안 되면 Esc). 반환: 닫힘 여부."""
    if not await ev(page, JS_POPUP_SHOWN):
        return True
    btn = page.locator(POPUP_CLOSE).first
    try:
        if await btn.count() and await btn.is_visible():
            await btn.click(timeout=TIMEOUTS["click"])
        else:
            await page.keyboard.press("Escape")
        await wf(page, "() => !(" + JS_POPUP_SHOWN + ")()", timeout=5000)
        return True
    except Exception as error:
        logging.debug("[POPUP] 팝업 닫기 실패: %s", type(error).__name__)
        return False


async def _real_check(page, code):
    """체크박스를 실제 클릭(라벨 포함)으로 한 번 더 누른다 (DOM click 이 반영되지 않는 경우 대비)."""
    box = page.locator(f'input.js-pfv2-compare-cta[data-modelcode="{code}" i]').first
    if not await box.count() or await box.is_checked():
        return
    try:
        label = page.locator(f'label[for="{await box.get_attribute("id")}"]').first if await box.get_attribute("id") else None
        target = label if label is not None and await label.count() else box
        await target.scroll_into_view_if_needed(timeout=TIMEOUTS["click"])
        await target.click(timeout=TIMEOUTS["click"], force=True)
    except Exception as error:
        logging.debug("[POPUP] 체크박스 실제 클릭 실패 %s: %s", code, type(error).__name__)


async def open_compare(page, codes):
    """체크 → Compare 버튼 → 새 팝업 → Key Specs 반영 대기.
    반환 dict: kind('OK' | V_BUTTON | V_NOPOPUP), state, columns, apis, what"""
    await close_popup(page)
    await dismiss_cookie_banner(page)
    checked = await ev(page, JS_SET_CHECKS, codes)
    try:
        await wf(page, JS_BUTTON_READY, codes, timeout=TIMEOUTS["button_ready"])
    except PlaywrightTimeout:
        for c in codes:
            await _real_check(page, c)
        try:
            await wf(page, JS_BUTTON_READY, codes, timeout=10000)
        except PlaywrightTimeout:
            d = await ev(page, JS_BUTTON_DIAG, codes)
            what = (f"Compare 버튼에 3개 SKU 가 반영되지 않음 (버튼 SKU '{d['button']}', 비활성 {d['disabled']}, "
                    f"체크된 SKU {d['checked']}, 체크박스 존재 {dict(zip(codes, d['boxes']))}"
                    f"{', 화면 메시지 ' + str(d['msgs']) if d['msgs'] else ''})")
            return {"kind": V_BUTTON, "state": {}, "columns": [], "apis": [], "what": what, "checked": checked}

    # Compare 버튼 → 팝업. 판정은 'API 주소' 가 아니라 '새 Compare 팝업이 실제로 뜨는지' 로 한다.
    apis = []

    def on_response(r):
        if "/compare" in r.url and r.request.resource_type in ("xhr", "fetch"):
            apis.append(f"{r.status} {r.url.split('?')[0].rsplit('/product/', 1)[-1]}")
    page.on("response", on_response)
    try:
        btn = page.locator("button.js-pfv2-open-compare-layer").locator("visible=true").first
        try:   # 실제 사용자처럼 화면 클릭 → 가려져 있으면 DOM click
            await btn.click(timeout=TIMEOUTS["click"])
        except Exception:
            await ev(page, "() => document.querySelector('button.js-pfv2-open-compare-layer').click()")
        try:
            await wf(page, JS_POPUP_READY, len(codes), timeout=TIMEOUTS["popup"])
            new_popup = True
        except PlaywrightTimeout:
            new_popup = False
        if new_popup:
            # 팝업 카드(제품명/SKU)는 PF 데이터로 먼저 그려지고 Key Specs 는 master/compare 응답 뒤에 채워진다.
            # 응답 전에 읽으면 섹션이 아직 없어 '미노출' 로 오판 (2026-11-10 09:41 실행, 15행) → 응답 + 반영을 기다림.
            # Compare 응답이 10초 안에 하나도 없으면(캐시 등) 더 기다리지 않는다.
            loop = asyncio.get_event_loop()
            start = loop.time()
            while not any("master/compare" in a for a in apis) and loop.time() - start < TIMEOUTS["api"] / 1000:
                if not apis and loop.time() - start > 10:
                    break
                await asyncio.sleep(0.2)
            try:
                await wf(page, JS_FEATURE_DRAWN, timeout=TIMEOUTS["keyspec_settle"])
            except PlaywrightTimeout:
                pass
    finally:
        page.remove_listener("response", on_response)
    if not new_popup:
        legacy = await ev(page, JS_LEGACY_POPUP)
        what = (f"기존 Compare 팝업이 열림 ({legacy})" if legacy else "Compare 팝업이 열리지 않음")
        return {"kind": V_NOPOPUP, "state": {}, "columns": [], "apis": apis, "what": what, "checked": checked}
    state, columns = await read_state(page, POPUP_ROOT)
    return {"kind": "OK", "state": state, "columns": columns, "apis": apis, "what": "", "checked": checked}


def verdicts_of(outcome, products):
    """제품별 상태 (common/substitute 의 V_*). 반환: [(verdict, 열 정보 또는 None)]"""
    if outcome["kind"] != "OK":
        return [(outcome["kind"], None) for _ in products]
    by_sku = {c["sku"].strip().upper(): c for c in outcome["columns"] if c.get("sku")}
    out = []
    for p in products:
        col = by_sku.get(p.rep_model.upper())
        out.append((V_NOSKU, None) if not col else (column_verdict(outcome["state"], col["col"])[0], col))
    return out


async def realize(page, retry, label):
    """retry.products 가 모두 PF 체크박스로 잡히게 한다 (안 되면 같은 자리를 다음 후보로). 반환: 성공 여부."""
    for _ in range(40):
        if retry.rounds and time.monotonic() > retry.deadline:   # 처음 확인은 항상 하고, 대체 중에는 시간 한도를 지킨다
            retry.stop_reason = "Set 처리 시간 한도"
            logging.warning("[POPUP] %s 대체 중단 - %s", label, retry.stop_reason)
            return False
        codes = [p.rep_model for p in retry.products]
        found = set((await ev(page, JS_FOUND, codes))["found"])
        missing = [i for i, c in enumerate(codes) if c not in found]
        if not missing:
            return True
        need = [codes[i] for i in missing]
        got = set((await load_until_found(page, codes))[0])     # 새로 연 PF 면 카드를 다시 불러와야 함
        if not set(need) <= got:
            got2, _notes = await switch_to_codes(page, [c for c in need if c not in got],
                                                 protect=[c for c in codes if c in got])
            got |= set(got2)
        still = [i for i in missing if codes[i] not in got]
        if not still:
            return True
        for i in still:
            nxt = retry.unavailable(i, retry.products[i], "PF 체크박스 확보 실패")
            if nxt is None:
                logging.warning("[POPUP] %s 대체 후보 소진 (PF 체크박스 확보 실패)", label)
                return False
    return False


def _api_note(apis):
    return f"Compare API {', '.join(apis[:3])}" if apis else "Compare API 응답 없음"


async def check_set(session, ctx, target, ps, used=()):
    page = await ctx.new_page()
    label = f"{target.site} {ps.division} {ps.set_no}"
    try:
        pf_url, status, tried, final, replaced, pool = await find_pf(session, page, target, ps, used)
        if not pf_url:
            return set_rows(target, PAGE_POPUP, ps, RESULT_CHECK,
                            "PF 에서 3개 제품 체크박스를 모두 찾지 못함 (시도: " + ", ".join(tried) + ")")
        retry = KeySpecRetry(final, pool, KEYSPEC_SUBSTITUTE_ROUNDS, POPUP_SET_BUDGET_S, label)
        retry.replaced.update(replaced)
        reloaded, last = False, None          # last = (확인한 제품, 결과, 제품별 상태) - 마지막으로 실제 확인한 상태

        async def fresh_pf(outcome):
            if outcome["kind"] != "OK" or not await close_popup(page):
                await session.goto(page, pf_url)        # 기존 팝업/닫히지 않는 팝업 → PF 를 새로 열어 깨끗한 상태로
                await wait_checkboxes(page)

        async def evaluate():
            outcome = await open_compare(page, [p.rep_model for p in retry.products])
            return list(retry.products), outcome, verdicts_of(outcome, retry.products)

        realize_failed = False
        while True:
            if not await realize(page, retry, label):
                if last is None:
                    return set_rows(target, PAGE_POPUP, ps, RESULT_CHECK,
                                    f"PF 에서 제품을 찾았으나 체크박스를 다시 확보하지 못함 | {retry.summary()} | PF {pf_url}")
                # 대체 후보를 화면에 올리지 못함 → 마지막으로 확인한 제품 상태로 기록
                retry.stop_reason = retry.stop_reason or "대체 후보를 PF 에서 체크하지 못함"
                realize_failed = True
                break
            last = await evaluate()
            outcome, pairs = last[1], last[2]
            if outcome["kind"] in (V_NOPOPUP, V_BUTTON) and not reloaded:
                # 일시적인 경우를 먼저 배제: PF 를 새로 열어 같은 제품으로 한 번 더 (제품 교체 전)
                reloaded = True
                logging.info("[POPUP] %s %s → PF 새로고침 후 같은 제품으로 재확인", label, outcome["what"])
                await session.goto(page, pf_url)
                await wait_checkboxes(page)
                continue
            if not retry.decide([v for v, _ in pairs]):
                break
            await fresh_pf(outcome)
        # 원인 찾기 중 바꿔 본 후보로도 해결되지 않아 원래 제품으로 되돌린 경우: 결과는 원래 제품 기준으로 남긴다
        # (원래 제품이 결함 후보이므로 결과 행에서 사라지지 않게 함)
        if (not realize_failed and [p.rep_model for p in retry.products] != [p.rep_model for p in last[0]]):
            await fresh_pf(last[1])
            if await realize(page, retry, label):
                last = await evaluate()
        retry.finalize(last[0])
        _, outcome, pairs = last
        ps = dataclasses.replace(ps, products=last[0])
        summary = retry.summary()
        tail = (" | " + summary) if summary else ""
        rows, shot = [], ""
        if outcome["kind"] != "OK" or any(v != "PASS" for v, _ in pairs):
            shot = await snap(page, f"popup_{label}_{outcome['kind']}")
        shot_note = f" | 캡처 {shot}" if shot else ""
        for p, (v, col) in zip(ps.products, pairs):
            prefix = replaced_note(p, retry.replaced)
            if outcome["kind"] == V_NOPOPUP:
                rows.append(make_row(target, PAGE_POPUP, ps, p, http=status, result=RESULT_CHECK,
                                     note=prefix + f"PF 에서 제품 찾음·체크 완료, 그러나 새 Compare 팝업(pdd43-master-compare) 이 "
                                     f"뜨지 않아 Key spec 판정 불가 - {outcome['what']} | {_api_note(outcome['apis'])} | "
                                     f"PF {pf_url}{tail}{shot_note}"))
            elif outcome["kind"] == V_BUTTON:
                rows.append(make_row(target, PAGE_POPUP, ps, p, http=status, result=RESULT_CHECK,
                                     note=prefix + f"PF 에서 제품 찾음·체크 시도, 그러나 {outcome['what']} - 팝업을 열 수 없어 "
                                     f"Key spec 판정 불가 | PF {pf_url}{tail}{shot_note}"))
            elif v == V_NOSKU:
                skus = [c["sku"] for c in outcome["columns"] if c.get("sku")]
                rows.append(make_row(target, PAGE_POPUP, ps, p, http=status, result=RESULT_CHECK,
                                     note=prefix + f"새 팝업은 열렸으나 팝업에서 SKU {p.rep_model} 열을 찾지 못함 "
                                     f"(팝업 SKU: {skus}) | {_api_note(outcome['apis'])}{tail}{shot_note}"))
            else:
                result, detail = keyspec_result(
                    outcome["state"], col["col"], RESULT_CHECK,
                    "Key Specs 섹션 미노출 - 세트 중 Key Specs 없는 제품이 있으면 섹션 전체 미노출이 규칙")
                rows.append(make_row(target, PAGE_POPUP, ps, p, final_text=col["name"], final_sku=col["sku"],
                                     http=status, result=result,
                                     note=prefix + f"{col['col']}열 {detail} | {_api_note(outcome['apis'])} | PF {pf_url}"
                                     f"{tail}{shot_note if result != 'PASS' else ''}"))
        logging.info("[POPUP] %s 체크 %s → %s", label, outcome.get("checked"), [r["Key spec 존재 결과"] for r in rows])
        return rows
    finally:
        await page.close()


async def check_division(session, target, division, sets):
    """사업부 워커: Set 1 → Set 2 순서로 처리하고 행만 돌려준다 (Excel 은 쓰지 않음).
    Set 마다 새 Context: 같은 p6 origin 의 Compare 선택 상태(localStorage 등)가 다음 Set 으로 넘어가지 않게 한다.
    Set 안에서 예외(페이지 재이동 등)가 나면 그 Set 만 새 Context 로 다시 시도하고, 그래도 안 되면 그 Set 만 '확인 필요'."""
    rows, used = [], set()       # used: 앞 Set 에서 쓴 SKU (대체 후보 중복 방지)
    for ps in sets:
        set_result = None
        for attempt in range(1, SET_ATTEMPTS + 1):
            try:
                async with session.worker_context() as ctx:
                    set_result = await check_set(session, ctx, target, ps, used)
                break
            except PlaywrightTimeout as error:   # 화면 조건 미충족 → 이 Set 만 '확인 필요', 다음 Set 계속
                note = timeout_note(error)
            except Exception as error:
                if type(error).__name__ == "SessionError":
                    raise                         # 로그인 문제는 국가 단위 재시도로
                note = f"검증 중 오류: {type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}"
            logging.warning("[POPUP] %s %s %s 시도 %d/%d 실패 - %s", target.site, division, ps.set_no, attempt,
                            SET_ATTEMPTS, note)
            set_result = set_rows(target, PAGE_POPUP, ps, RESULT_CHECK, f"{note} (Set {SET_ATTEMPTS}회 시도)")
        used.update(r["SKU"] for r in set_result if r.get("SKU"))
        rows += set_result
    return rows


if __name__ == "__main__":
    entry(PAGE_POPUP, check_division, POPUP_OUTPUT, "PF POPUP Page Key Spec Checker")
