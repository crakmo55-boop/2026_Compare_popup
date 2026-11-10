# -*- coding: utf-8 -*-
"""[Entry Point 1] Landing Page (Compare models 페이지) Key Spec Checker.

공통 흐름(common/runner.py): base_urls → 국가/환경 → 로그인(p6-pre-qa3)/생략(Live) → 매핑 → Product Set
이 파일: 사업부(MX/VD/DA)마다 병렬로, Set 마다 새 Context 에서
  Compare 페이지 열기 → 열 1·2·3 드롭다운에서 Set 의 제품 선택(열 SKU 로 확인) → 제품별 Key Specs 확인
  → Key Specs 가 없으면(열 값 없음 / 섹션 미노출) 같은 사업부·같은 카테고리의 다른 매핑 제품으로 바꿔 다시 확인
    (common/substitute.py, 2026-11-10 사용자 지시)

판정은 기존 run_c_landing_live.py 와 같음
  - 섹션 노출 + 해당 열 값 있음 → PASS / 값 없음 → FAIL / 섹션 미노출 → FAIL
HTTP 상태 = Compare 페이지 이동(메인 문서) 응답 코드.
CTA(Buy / Learn more)는 누르지 않는다 (Key spec 존재 확인만).

드롭다운 선택 방식 (2026-11-10 재설계, 근거: 11/10 13:07 실행 결과)
  - 드롭다운 옵션 이름은 현지어 표시명이라 매핑 영문명(FMY_MKT_NM)과 다르다 (KZ/RU/LV/LT/NO/RO/CH 등 '비슷한 옵션 없음' 49행)
    → Popup 결과의 '최종 페이지 랜딩 후 제품 텍스트'(같은 pdd43 컴포넌트의 현지어 표시명)를 1순위 단서로 쓰고,
      그래도 없으면 옵션을 직접 골라 열 SKU 를 읽어 확인한다 (옵션 → SKU 기록은 같은 페이지에서 재사용).
  - 옵션 순번(index) 으로 누르지 않는다. 드롭다운을 연 뒤 '화면에 보이는' 옵션 중 글자가 같은 것을 그 자리에서 찾아 누른다
    (AL/ES: 순번으로 누른 옵션과 다른 제품이 선택된 사례 → 다시 그려진 목록/복제 목록의 순번 불일치 방지).
  - 성공 기준은 오직 '열 SKU == 매핑 SKU'. 선택 시간은 열·Set 마다 한도를 둔다 (ES 1446초, AL 1586초 재발 방지).
"""
import dataclasses
import logging
import re
import time

from playwright.async_api import TimeoutError as PlaywrightTimeout

from common.config import (CATEGORY_SPECS, DIVISION_PAGES, KEYSPEC_SUBSTITUTE_ROUNDS, LANDING_OUTPUT,
                           LANDING_SCAN_MAX, LANDING_SELECT_BUDGET_S, LANDING_SET_BUDGET_S, PAGE_LANDING,
                           POPUP_OUTPUT, POPUP_PRODUCTS_MAX_AGE_HOURS, RESULT_CHECK, RESULT_DIR, RESULT_FAIL,
                           RESULT_PASS, SET_ATTEMPTS, SET_SIZE, TIMEOUTS, USE_POPUP_PRODUCTS)
from common.keyspec_page import (column_verdict, dismiss_cookie_banner, ev, keyspec_result, norm, read_state, snap,
                                 timeout_note, wf)
from common.result import make_row, replaced_note, set_rows
from common.runner import entry
from common.substitute import V_PASS, KeySpecRetry

ROOT = ".pdd43-master-compare"
V_NOSEL = "NOSEL"            # 자동화가 이 열에 제품을 띄우지 못함 (Key spec 판정 대상 아님)

# 화면에 보이는 루트 / 열 카드 (복제 카드가 있어도 보이는 것)
_JS_CARD = r"""
  const vis = e => !!(e && e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'
                      && e.getBoundingClientRect().height > 0);
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const roots = [...document.querySelectorAll('.pdd43-master-compare')];
  const root = roots.find(vis) || roots[0];
  const cardsOf = n => root ? [...root.querySelectorAll(`li.pdd43-master-compare__card-item.pdd43-master-compare__column--${n}`)] : [];
  const cardOf = n => { const c = cardsOf(n); return c.find(vis) || c[0] || null; };
  const menuOf = f => f.closest('.menu') || f.parentElement;
  const optsOf = f => { const m = menuOf(f); return [...m.querySelectorAll('.menu__list-option')]
                          .filter(o => (o.closest('.menu') || m) === m); };
"""

# 실패 진단: Compare 영역 / 카드 / 드롭다운 구조 요약 (비고·로그 기록용, 실제 p6 구조 확인 목적)
JS_DIAG = r"""() => {""" + _JS_CARD + r"""
  const cards = root ? [...root.querySelectorAll('li.pdd43-master-compare__card-item')] : [];
  return {title: document.title.slice(0, 60), url: location.pathname, root: !!root, roots: roots.length, cards: cards.length,
          cardCols: cards.map(c => (String(c.className).match(/column--(\d+)/) || [])[1]).join(','),
          skus: cards.map(c => clean((c.querySelector('.pdd43-master-compare__sku-text') || {}).textContent)).slice(0, 6),
          selects: root ? root.querySelectorAll('button.menu__select-field').length : 0,
          options: root ? root.querySelectorAll('.menu__list-option').length : 0};
}"""

# 열 n 카드의 드롭다운들 (닫힌 상태에서도 textContent 로 읽음). 필드에는 클릭용 표시(data-ks-field)를 다시 붙인다.
JS_FIELDS = r"""(n) => {""" + _JS_CARD + r"""
  const card = cardOf(n);
  if (!card) return null;
  document.querySelectorAll(`[data-ks-field^="${n}-"]`).forEach(e => e.removeAttribute('data-ks-field'));
  return [...card.querySelectorAll('button.menu__select-field')].map((f, d) => {
    f.setAttribute('data-ks-field', `${n}-${d}`);
    return {d, visible: vis(f), field: clean(f.textContent).slice(0, 80), expanded: f.getAttribute('aria-expanded'),
            options: optsOf(f).map((o, i) => ({i, text: clean(o.textContent),
                     disabled: o.getAttribute('aria-disabled') === 'true' || /disabled/i.test(String(o.className)),
                     values: [...o.attributes].map(a => a.value)
                              .concat([...o.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)))}))};
  });
}"""

# 연 드롭다운에서 글자가 text 인 k 번째 옵션을 찾아 클릭 표시(data-ks-pick). 화면에 보이는 옵션 우선
# (목록이 body 아래 다른 곳에 그려지는 경우 포함), 없으면 그 드롭다운 안의 옵션.
JS_PICK = r"""([n, d, text, k]) => {""" + _JS_CARD + r"""
  document.querySelectorAll('[data-ks-pick]').forEach(e => e.removeAttribute('data-ks-pick'));
  const card = cardOf(n); if (!card) return {ok: false, why: 'no-card'};
  const f = [...card.querySelectorAll('button.menu__select-field')][d]; if (!f) return {ok: false, why: 'no-field'};
  const own = optsOf(f);
  const shown = [...document.querySelectorAll('.menu__list-option')].filter(vis);
  let pool = own.filter(vis);
  if (!pool.length && f.getAttribute('aria-expanded') === 'true') pool = shown;
  if (!pool.length) pool = own;
  const hits = pool.filter(o => clean(o.textContent) === text);
  const o = hits[k] || null;
  if (!o) return {ok: false, why: 'no-match', pool: pool.length, hits: hits.length};
  o.setAttribute('data-ks-pick', '1');
  return {ok: true, hits: hits.length, visible: vis(o), disabled: o.getAttribute('aria-disabled') === 'true' || /disabled/i.test(String(o.className))};
}"""

JS_ANY_OPTION_SHOWN = r"""() => [...document.querySelectorAll('.menu__list-option')].some(e => e.getClientRects().length
                                && getComputedStyle(e).visibility !== 'hidden' && e.getBoundingClientRect().height > 0)"""

JS_COLUMN_SKU = r"""(n) => {""" + _JS_CARD + r"""
  const card = cardOf(n);
  const e = card && card.querySelector('.pdd43-master-compare__sku-text');
  return e ? clean(e.textContent) : null;
}"""

JS_FIELD_TEXT = r"""([n, d]) => {""" + _JS_CARD + r"""
  const card = cardOf(n); const f = card && [...card.querySelectorAll('button.menu__select-field')][d];
  return f ? clean(f.textContent) : null;
}"""

JS_CARD_HTML = r"""(n) => {""" + _JS_CARD + r"""
  const card = cardOf(n); return card ? card.outerHTML.replace(/\s+/g, ' ').slice(0, 900) : '';
}"""

JS_FEATURE_DRAWN = r"""() => { const r = [...document.querySelectorAll('.pdd43-master-compare')]
    .find(e => e.getClientRects().length) || document.querySelector('.pdd43-master-compare');
  const f = r && r.querySelector('.pdd43-master-compare__feature');
  return !!(f && f.offsetParent !== null && f.querySelector('.pdd43-master-compare__feature-item')); }"""


async def diag(page) -> str:
    try:
        d = await ev(page, JS_DIAG)
    except Exception as error:
        return f"진단 실패 {type(error).__name__}"
    return (f"진단: 경로 {d['url']}, 제목 '{d['title']}', Compare 루트 {d['roots']}개, 카드 {d['cards']}개(열 {d['cardCols']}), "
            f"열 SKU {d['skus']}, 드롭다운 {d['selects']}개, 옵션 {d['options']}개")


def landing_candidates(ps):
    """Set 카테고리(ps.page_key)가 정해져 있으면 그 Compare 페이지 하나만, 아니면 사업부 페이지 전체."""
    keys = (ps.page_key,) if ps.page_key else DIVISION_PAGES[ps.division]
    return [spec for spec in CATEGORY_SPECS if spec[1] in keys]


# ------------------------------------------------------------------ 이름 유사도 (단서 순서 정하기용 - 성공 판정은 SKU)
_STOP = {"samsung", "vision", "ai", "smart", "tv", "the", "with", "and", "de", "con", "com", "da", "do", "e", "y", "et",
         "inch", "size", "new", "only"}
_SIZE_PLACEHOLDER = re.compile(r"\{\s*(size|inch)\s*\}", re.I)


def _key(text) -> str:
    """비교용 정규화: 소문자 + 문자·숫자만 (괄호·쉼표·공백·특수기호·zero-width 차이 무시)."""
    return "".join(ch for ch in norm(text).lower() if ch.isalnum())


def _size_from_sku(code) -> str:
    """TV 계열 SKU 의 화면 크기 (UE65U8000HKXXU → 65, MRE115MR95FXXU → 115). 없으면 ''."""
    m = re.match(r"^[A-Z]{2,3}(\d{2,3})[A-Z]", code or "")
    return m.group(1) if m else ""


def family(code) -> str:
    """SKU 의 모델 계열 (크기·용량·지역 접미사 제외). 옵션 탐색 순서 힌트로만 사용.
    SM-S741BZKGEUC → SM-S741 / QE65S93HAEXXC → QE|S93HA / WW90DB7U34GWU3 → WW|DB7U3"""
    c = str(code or "").upper()
    if c.startswith("SM-"):
        return c[:7]
    m = re.match(r"^([A-Z]{1,4})(\d{2,3})([A-Z0-9]{0,5})", c)
    return f"{m.group(1)}|{m.group(3)}" if m else c[:7]


def _tokens(text):
    text = (text or "").replace("™", " ").replace("®", " ").replace("​", " ")
    return [t for t in re.findall(r"\w+", text.lower()) if t not in _STOP and t != "_"]


def _weight(tok) -> float:
    if re.search(r"[^\W\d_]", tok) and re.search(r"\d", tok):
        return 3.0          # 모델 코드성 토큰 (u7000h, qn990h, s11, 11kg)
    if tok.isdigit():
        return 2.0          # 크기·용량 숫자 (65, 323)
    return 1.0


def text_score(option_text, names, size="") -> float:
    """드롭다운 옵션 텍스트와 이름들(Popup 화면명 / 매핑명)의 유사도 (0~1). 가중 토큰 F1 의 최댓값."""
    opt = _tokens(option_text)
    if not opt:
        return 0.0
    best = 0.0
    for name in names:
        if not name:
            continue
        prod = _tokens(_SIZE_PLACEHOLDER.sub(size, name))
        if size and size not in prod:
            prod.append(size)
        if not prod:
            continue
        common = set(opt) & set(prod)
        c = sum(_weight(t) for t in common)
        a = sum(_weight(t) for t in set(opt))
        b = sum(_weight(t) for t in set(prod))
        best = max(best, 2 * c / (a + b) if a + b else 0.0)
    return best


def option_score(option_text, product, hints=()) -> float:
    """(호환) 옵션 텍스트와 제품의 유사도. hints = Popup 화면 표시명 등 현지어 이름."""
    return text_score(option_text, [*hints, product.fmy_mkt, product.fmy_eng], _size_from_sku(product.rep_model))


MIN_SCORE = 0.45


def rank_options(options, product, hints=()):
    """제품에 맞을 법한 옵션 순서. 속성값에 SKU 가 있으면 맨 앞, Popup 표시명과 글자가 같으면 그다음. 점수 MIN_SCORE 미만 제외."""
    code = product.rep_model.lower()
    hint_keys = {_key(h) for h in hints if h}
    exact = [o for o in options if any(str(v).strip().lower() == code for v in o["values"])]
    same = [o for o in options if o not in exact and _key(o["text"]) in hint_keys]
    scored = sorted(((option_score(o["text"], product, hints), o) for o in options if o not in exact and o not in same),
                    key=lambda t: -t[0])
    return exact + same + [o for s, o in scored if s >= MIN_SCORE]


def split_dropdowns(dds):
    """카드 드롭다운을 '제품 목록' 과 '용량/사이즈/색상 옵션' 으로 나눈다.
    2026-11-09 ES TV: 두 번째 드롭다운이 같은 제품 목록(104개)의 복제본 → 옵션으로 오인해 엉뚱한 제품 선택 + 10분 소요.
    - 첫 드롭다운과 옵션 텍스트가 절반 이상 겹치면 같은 제품 목록(복제본)
    - 제품 목록이 여러 개면 화면에 보이는 것을 사용
    - 나머지 중 옵션 30개 이하만 용량/사이즈/색상 옵션으로 사용"""
    if not dds:
        return None, []
    base = {_key(o["text"]) for o in dds[0]["options"] if o["text"]}

    def same_list(dd):
        keys = {_key(o["text"]) for o in dd["options"] if o["text"]}
        return bool(base) and len(keys & base) >= 0.5 * max(1, min(len(keys), len(base)))
    product_lists = [dd for dd in dds if dd is dds[0] or same_list(dd)]
    primary = next((dd for dd in product_lists if dd.get("visible") and dd["options"]),
                   next((dd for dd in product_lists if dd["options"]), product_lists[0]))
    variants = [dd for dd in dds if dd not in product_lists and 0 < len(dd["options"]) <= 30 and dd.get("visible", True)]
    return primary, variants


# ------------------------------------------------------------------ 드롭다운 조작
async def _sku(page, n) -> str:
    return (await ev(page, JS_COLUMN_SKU, n) or "").strip().upper()


async def read_fields(page, n):
    """열 n 의 드롭다운들. 제품 목록 옵션이 비어 있으면(열 때 그려지는 경우) 한 번 열어서 다시 읽는다."""
    dds = await ev(page, JS_FIELDS, n) or []
    primary, _ = split_dropdowns(dds)
    if primary is not None and not primary["options"]:
        await _open(page, n, primary["d"])
        dds = await ev(page, JS_FIELDS, n) or []
        await page.keyboard.press("Escape")
    return dds


async def _open(page, n, d) -> bool:
    """드롭다운을 연다 (실제 클릭). 보이는 옵션이 나타나면 True."""
    field = page.locator(f'[data-ks-field="{n}-{d}"]').first
    try:
        if await field.get_attribute("aria-expanded") != "true":
            await field.scroll_into_view_if_needed(timeout=TIMEOUTS["click"])
            await field.click(timeout=TIMEOUTS["click"])
        await wf(page, JS_ANY_OPTION_SHOWN, timeout=3000)
        return True
    except Exception as error:   # 가려짐/시간 초과 → 닫힌 목록에서 고르는 방식으로 계속
        logging.debug("[LANDING] %s열 드롭다운 열기 실패: %s", n, type(error).__name__)
        return False


async def choose(page, n, d, text, k=0):
    """열 n 의 d 번째 드롭다운에서 글자가 text 인 k 번째 옵션을 고른다.
    반환: (반영 여부, 설명). 반영 = 열 SKU 가 바뀌었거나 드롭다운 표시 글자가 text 와 같아짐."""
    want = _key(text)
    await ev(page, JS_FIELDS, n)                       # 클릭 표시 다시 붙임 (다시 그려졌을 수 있음)
    before = await _sku(page, n)
    # 같은 이름 옵션이 하나뿐이고 이미 그 옵션이 선택돼 있으면 누르지 않는다.
    # (선택된 옵션을 다시 누르면 카드가 비동기로 다시 그려져, 바로 다음 클릭 대상이 사라지는 문제 방지)
    first = await ev(page, JS_PICK, [n, d, text, k])
    if first.get("ok") and first.get("hits") == 1 and _key(await ev(page, JS_FIELD_TEXT, [n, d])) == want:
        return True, "이미 선택된 옵션"
    how = "click"
    for attempt in (1, 2):
        opened = await _open(page, n, d)
        pick = await ev(page, JS_PICK, [n, d, text, k])
        if not pick.get("ok"):
            await page.keyboard.press("Escape")
            return False, f"옵션 '{text[:30]}' 없음({pick.get('why')})"
        if pick.get("disabled"):
            await page.keyboard.press("Escape")
            return False, f"옵션 '{text[:30]}' 비활성(다른 열에 선택된 제품일 수 있음)"
        opt = page.locator('[data-ks-pick="1"]').first
        try:
            if not (opened and pick.get("visible")):
                raise RuntimeError("목록이 보이지 않음")
            await opt.scroll_into_view_if_needed(timeout=5000)
            await opt.click(timeout=5000)
            how = "click"
            break
        except Exception:
            try:
                if not await opt.count():
                    raise RuntimeError("옵션이 다시 그려져 사라짐")
                await opt.evaluate("e => e.click()")
                how = "dom"
                break
            except Exception as error:
                if attempt == 2:
                    return False, f"옵션 클릭 실패 {type(error).__name__}"
                await ev(page, JS_FIELDS, n)          # 다시 그려진 카드에 표시를 다시 붙이고 한 번 더
    try:
        await wf(page, "([n, before, want, d]) => { const s = (" + JS_COLUMN_SKU + ")(n); "
                       "const t = (" + JS_FIELD_TEXT + ")([n, d]); "
                       "const k = x => (x || '').toLowerCase().replace(/[^\\p{L}\\p{N}]/gu, ''); "
                       "return (s && s.toUpperCase() !== before) || k(t) === want; }",
                 [n, before, want, d], timeout=8000)
        applied = True
    except PlaywrightTimeout:
        applied = False
    if await page.locator(f'[data-ks-field="{n}-{d}"][aria-expanded="true"]').count():
        await page.keyboard.press("Escape")
    return applied, how


async def select_product(page, n, product, hints, cache, deadline):
    """열 n 에 product 를 띄운다. 성공 기준 = 열 SKU == REP_MODEL.
    순서: ① 옵션 속성값에 SKU ② 이 페이지에서 이미 SKU 를 확인한 옵션(같은 SKU → 같은 계열)
          ③ Popup 화면 표시명과 글자가 같은 옵션 ④ 이름 유사도 ⑤ 남은 옵션을 직접 골라 SKU 확인 (LANDING_SCAN_MAX 개까지)
    제품 옵션을 골랐는데 같은 계열 SKU 면 용량/사이즈/색상 드롭다운에서 SKU 를 맞춘다.
    반환: (성공 여부, 설명)"""
    code = product.rep_model.strip().upper()
    if await _sku(page, n) == code:
        return True, "이미 선택됨"
    deadline = min(deadline, time.monotonic() + LANDING_SELECT_BUDGET_S)
    primary, _ = split_dropdowns(await read_fields(page, n))
    if not primary or not primary["options"]:
        return False, f"{n}열 제품 드롭다운 옵션 없음 (드롭다운을 열어도 옵션이 그려지지 않음)"
    opts = primary["options"]
    seq, seen = [], set()

    def add(o):
        k = sum(1 for x in opts[:o["i"]] if x["text"] == o["text"])      # 같은 글자 옵션 중 몇 번째인지
        if (o["text"], k) not in seen and o["text"] and not o.get("disabled"):
            seen.add((o["text"], k))
            seq.append((o["text"], k))
    fam = family(code)
    for o in opts:                                                    # ① 속성값 SKU
        if any(str(v).strip().upper() == code for v in o["values"]):
            add(o)
    for o in opts:                                                    # ② 확인된 SKU
        if cache.get(o["text"]) == code:
            add(o)
    for o in opts:
        if cache.get(o["text"]) and family(cache[o["text"]]) == fam:
            add(o)
    for o in rank_options(opts, product, hints):                      # ③④
        add(o)
    strong = len(seq)
    rest = sorted((o for o in opts if o["text"] not in cache),
                  key=lambda o: -option_score(o["text"], product, hints))
    for o in rest[:LANDING_SCAN_MAX]:                                 # ⑤ 직접 확인
        add(o)

    tried = []
    for idx, (text, k) in enumerate(seq):
        if time.monotonic() > deadline:
            tried.append("시간 한도")
            break
        applied, how = await choose(page, n, primary["d"], text, k)
        sku = await _sku(page, n)
        if applied and sku:
            cache.setdefault(text, sku)
        tried.append(f"'{text[:28]}'→{sku or '-'}" + ("" if applied else f"(반영 안 됨: {how})"))
        if sku == code:
            return True, f"제품 옵션 '{text[:40]}'"
        if not applied or not (idx < strong or family(sku) == fam):
            continue
        # 같은 제품의 다른 용량/사이즈/색상일 수 있음 → 옵션 드롭다운에서 SKU 맞추기
        _, variants = split_dropdowns(await ev(page, JS_FIELDS, n) or [])
        for dd in variants:
            direct = [o for o in dd["options"] if any(str(v).strip().upper() == code for v in o["values"])]
            for o in (direct or dd["options"][:12]):
                if time.monotonic() > deadline:
                    break
                vk = sum(1 for x in dd["options"][:o["i"]] if x["text"] == o["text"])
                await choose(page, n, dd["d"], o["text"], vk)
                if await _sku(page, n) == code:
                    return True, f"제품 옵션 '{text[:40]}' + '{o['text'][:20]}'"
    return False, (f"{n}열 SKU {code} 를 맞추지 못함 (옵션 {len(opts)}개 중 {len(tried)}개 시도: "
                   f"{'; '.join(tried[:8])}{' …' if len(tried) > 8 else ''})")


# ------------------------------------------------------------------ 페이지 열기
async def open_compare_page(session, page, target, ps, tried):
    """사업부 Compare 페이지 중 새 Compare 컴포넌트가 있는 페이지를 CATEGORY_SPECS 순서로 돌려준다.
    tried 에 페이지별 결과를 누적한다 (비고 기록용)."""
    for _group, key, _label, _pf, cmp_path in landing_candidates(ps):
        url = f"{target.host}/{target.site}/{cmp_path}"
        resp = await session.goto(page, url)
        status = resp.status if resp else None
        tried.append(f"{key}={status}")
        if not status or status >= 400:
            continue
        # 새 Compare 컴포넌트는 서버 HTML 에 포함됨 → load 이후에도 루트가 없으면 미배포 페이지 (60초 헛대기 방지)
        await page.wait_for_load_state("load", timeout=TIMEOUTS["page_load"])
        try:   # 로드 직후 페이지가 스스로 다시 이동하는 경우(CA/CA_FR 'Execution context was destroyed') 를 흡수
            await page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeout:
            pass
        if not await page.locator(ROOT).count():
            tried[-1] += f"(새 Compare 영역 없음 | {await diag(page)})"
            continue
        try:
            await wf(page, "() => document.querySelectorAll('.pdd43-master-compare .pdd43-master-compare__sku-text').length > 0",
                     timeout=TIMEOUTS["element"])
        except PlaywrightTimeout:
            tried[-1] += f"(Compare 제품 카드 미표시 | {await diag(page)})"
            continue
        yield key, url, status


# ------------------------------------------------------------------ Popup 결과 재사용
_POPUP = None


def _popup_data():
    """최신 Popup 결과 → ({(국가, 사업부, Set): [SKU x3]}, {(국가, SKU): 화면 표시명}). SKU 매칭 PASS 행만."""
    global _POPUP
    if _POPUP is not None:
        return _POPUP
    _POPUP = ({}, {})
    if not USE_POPUP_PRODUCTS:
        return _POPUP
    files = sorted((f for f in RESULT_DIR.glob(f"{POPUP_OUTPUT.stem}_*.xlsx") if not f.name.startswith("~$")),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        logging.warning("[LANDING] Popup 결과 파일이 없어 Landing 이 제품을 직접 선정합니다.")
        return _POPUP
    latest = files[0]
    age_h = (time.time() - latest.stat().st_mtime) / 3600
    if age_h > POPUP_PRODUCTS_MAX_AGE_HOURS:
        logging.warning("[LANDING] 최신 Popup 결과 %s 가 %.1f시간 전 파일이라 사용하지 않습니다 (기준 %d시간).",
                        latest.name, age_h, POPUP_PRODUCTS_MAX_AGE_HOURS)
        return _POPUP
    import openpyxl
    wb = openpyxl.load_workbook(latest, read_only=True, data_only=True)
    try:
        rows = wb["Result_All"].iter_rows(values_only=True)
        header = list(next(rows))
        i = {c: header.index(c) for c in ("국가", "사업부", "Set", "SKU", "SKU 매칭 결과", "최종 페이지 랜딩 후 제품 텍스트")}
        for r in rows:
            if r[i["SKU 매칭 결과"]] == RESULT_PASS and r[i["SKU"]]:
                site = str(r[i["국가"]]).lower()
                _POPUP[0].setdefault((site, r[i["사업부"]], r[i["Set"]]), []).append(r[i["SKU"]])
                name = r[i["최종 페이지 랜딩 후 제품 텍스트"]]
                if name:
                    _POPUP[1][(site, str(r[i["SKU"]]).upper())] = str(name)
    finally:
        wb.close()
    logging.info("[LANDING] Popup 과 같은 제품으로 검증: %s (%.1f시간 전, Set %d개, 화면 표시명 %d개)", latest.name, age_h,
                 sum(1 for v in _POPUP[0].values() if len(v) == SET_SIZE), len(_POPUP[1]))
    return _POPUP


def popup_products():
    return _popup_data()[0]


def popup_name(site, code):
    return _popup_data()[1].get((site.lower(), str(code).upper()))


def apply_popup_products(target, ps):
    """Popup 에서 확정된 3개 제품이 있으면 그 제품으로 Set 을 바꾼다. 반환: (Set, 적용 여부)."""
    codes = popup_products().get((target.site.lower(), ps.division, ps.set_no))
    if not codes or len(codes) != SET_SIZE:
        return ps, False
    catalog = {p.rep_model: p for p in list(ps.products) + list(ps.alternates)}
    if not all(c in catalog for c in codes):
        return ps, False
    chosen = [catalog[c] for c in codes]
    rest = [p for p in ps.alternates if p.rep_model not in codes] + [p for p in ps.products if p.rep_model not in codes]
    return dataclasses.replace(ps, products=chosen, alternates=rest), True


# ------------------------------------------------------------------ Set 검증
_OPTION_SKU = {}          # (국가, 페이지) → {옵션 글자: 그 옵션을 골랐을 때 열 SKU}  (Set 1·2 가 같은 페이지면 재사용)


async def _assign_columns(page, products):
    """이미 어떤 열에 떠 있는 제품은 그 열에 둔다 (다른 열과 충돌·불필요한 선택 방지). 반환: 열 순서 제품 목록."""
    shown = [await _sku(page, n) for n in range(1, SET_SIZE + 1)]
    cols, rest = [None] * SET_SIZE, []
    for p in products:
        code = p.rep_model.upper()
        n = next((i for i, s in enumerate(shown) if s == code and cols[i] is None), None)
        if n is None:
            rest.append(p)
        else:
            cols[n] = p
    for i in range(SET_SIZE):
        if cols[i] is None and rest:
            cols[i] = rest.pop(0)
    return [p for p in cols if p is not None]


async def realize(page, retry, site, cache, how, fails):
    """retry.products[i] 를 (i+1)열에 띄운다. 못 띄우면 같은 자리를 다음 후보로 (후보가 없으면 그 열은 NOSEL).
    how[i] = 선택 설명, fails[i] = 실패 사유. 반환: 이번에 실패한 열 인덱스 목록."""
    failed = []
    for i in range(len(retry.products)):
        n = i + 1
        while True:
            p = retry.products[i]
            if time.monotonic() > retry.deadline:
                fails[i] = fails.get(i) or "Set 처리 시간 한도로 선택 중단"
                failed.append(i)
                break
            ok, desc = await select_product(page, n, p, [popup_name(site, p.rep_model)], cache, retry.deadline)
            if ok:
                how[i] = desc
                fails.pop(i, None)
                break
            fails[i] = desc
            if retry.unavailable(i, p, "Landing 드롭다운에서 선택하지 못함") is None:
                retry.products[i] = p                 # 후보 소진 → 원래 제품 자리 유지, 이 열은 판정 안 함
                failed.append(i)
                break
    # 한 열을 바꾸면 다른 열이 바뀌는 페이지 대비: 마지막에 전체 SKU 한 번 더 확인
    for i, p in enumerate(retry.products):
        if i not in failed and await _sku(page, i + 1) != p.rep_model.upper():
            ok, desc = await select_product(page, i + 1, p, [popup_name(site, p.rep_model)], cache, retry.deadline)
            if not ok:
                fails[i] = "다른 열을 고른 뒤 이 열 제품이 바뀜 - " + desc
                failed.append(i)
    return failed


async def check_set(session, ctx, target, ps, used=()):
    """Compare 페이지에서 열 1·2·3 에 Set 제품을 고르고 Key Specs 를 확인한다. Key Specs 가 없으면 대체 후 재확인."""
    page = await ctx.new_page()
    label = f"{target.site} {ps.division} {ps.set_no}"
    try:
        tried = []
        async for key, url, status in open_compare_page(session, page, target, ps, tried):
            await dismiss_cookie_banner(page)
            cache = _OPTION_SKU.setdefault((target.site, key), {})
            products = await _assign_columns(page, ps.products[:SET_SIZE])
            # 대체 후보: 같은 사업부·카테고리 매핑 후보 전체 (Popup 표시명/이름이 비슷한 후보 먼저, 나머지는 SKU 직접 확인)
            dds = await read_fields(page, 1)
            primary, _ = split_dropdowns(dds)
            options = primary["options"] if primary else []
            pool = [p for p in ps.alternates if p.rep_model not in used and p not in products]
            pool.sort(key=lambda p: 0 if rank_options(options, p, [popup_name(target.site, p.rep_model)]) else 1)
            retry = KeySpecRetry(products, pool, KEYSPEC_SUBSTITUTE_ROUNDS, LANDING_SET_BUDGET_S, label)
            how, fails, last = {}, {}, None

            async def evaluate():
                failed = await realize(page, retry, target.site, cache, how, fails)
                try:   # Key Specs 는 열 선택 뒤 다시 그려짐 → 반영을 기다림 (정말 없으면 끝까지 안 보임)
                    await wf(page, JS_FEATURE_DRAWN, timeout=TIMEOUTS["keyspec_settle"])
                except PlaywrightTimeout:
                    pass
                state, columns = await read_state(page, ROOT)
                by_col = {c["col"]: c for c in columns}
                verdicts = []
                for i, p in enumerate(retry.products):
                    col = by_col.get(i + 1, {})
                    if i in failed or (col.get("sku") or "").strip().upper() != p.rep_model.upper():
                        verdicts.append(V_NOSEL)
                    else:
                        verdicts.append(column_verdict(state, i + 1)[0])
                return list(retry.products), state, by_col, verdicts, dict(how), dict(fails)

            while True:
                last = await evaluate()
                # 판정 대상 열만으로 대체 여부 결정 (NOSEL 열은 후보가 이미 소진된 상태)
                if not retry.decide([V_PASS if v == V_NOSEL else v for v in last[3]]):
                    break
            # 원인 찾기 중 바꿔 본 후보로도 해결되지 않아 원래 제품으로 되돌린 경우 → 원래 제품 기준으로 다시 확인해 기록
            if [p.rep_model for p in retry.products] != [p.rep_model for p in last[0]]:
                last = await evaluate()
            retry.finalize(last[0])
            evaluated, state, by_col, verdicts, how, fails = last
            final_ps = dataclasses.replace(ps, products=evaluated)
            summary = retry.summary()
            tail = (" | " + summary) if summary else ""
            shot = ""
            if any(v != V_PASS for v in verdicts):
                shot = await snap(page, f"landing_{label}")
            shot_note = f" | 캡처 {shot}" if shot else ""
            rows = []
            for i, (product, v) in enumerate(zip(evaluated, verdicts)):
                n = i + 1
                col = by_col.get(n, {})
                prefix = replaced_note(product, retry.replaced)
                if v == V_NOSEL:
                    rows.append(make_row(target, PAGE_LANDING, final_ps, product, final_text=col.get("name", ""),
                                         final_sku=col.get("sku", ""), http=status, result=RESULT_CHECK,
                                         sku_result=RESULT_CHECK,
                                         note=prefix + f"{fails.get(i, '선택한 열의 SKU 가 다름')} - 자동화가 이 제품을 열에 "
                                         f"띄우지 못해 다른 제품이 표시된 상태라 Key spec 판정 안 함 | {url}{tail}{shot_note}"))
                    continue
                result, detail = keyspec_result(state, n, RESULT_FAIL, "Key Specs 섹션 미노출")
                rows.append(make_row(target, PAGE_LANDING, final_ps, product, final_text=col.get("name", ""),
                                     final_sku=col.get("sku", ""), http=status, result=result,
                                     note=prefix + f"{n}열 {detail} ({how.get(i, '')}) | {url}{tail}"
                                     f"{shot_note if result != RESULT_PASS else ''}"))
            logging.info("[LANDING] %s → %s", label, [r["Key spec 존재 결과"] for r in rows])
            bad = [i for i, v in enumerate(verdicts) if v == V_NOSEL]
            if bad:
                html = await ev(page, JS_CARD_HTML, bad[0] + 1)
                logging.warning("[LANDING] %s 선택 실패 %d열 - %s | %s | 카드 HTML: %s", label, len(bad),
                                fails.get(bad[0], ""), await diag(page), html)
            return rows
        note = "Compare 페이지를 열지 못함 (시도: " + ", ".join(tried) + ")"
        logging.warning("[LANDING] %s 확인 필요 - %s", label, note)
        return set_rows(target, PAGE_LANDING, ps, RESULT_CHECK, note)
    finally:
        await page.close()


async def check_division(session, target, division, sets):
    """사업부 워커: Set 1 → Set 2 순서로 처리하고 행만 돌려준다 (Excel 은 쓰지 않음).
    Set 마다 새 Context, Set 안 예외는 그 Set 만 다시 시도 (popup_checker.check_division 과 같은 규칙)."""
    rows, used = [], set()       # used: 앞 Set 에서 쓴 SKU (대체 후보 중복 방지)
    for ps in sets:
        ps, from_popup = apply_popup_products(target, ps)
        if from_popup:
            logging.info("[LANDING] %s %s %s Popup 과 같은 제품 사용 %s", target.site, ps.division, ps.set_no,
                         [p.rep_model for p in ps.products])
        set_result = None
        for attempt in range(1, SET_ATTEMPTS + 1):
            try:
                async with session.worker_context() as ctx:
                    set_result = await check_set(session, ctx, target, ps, used)
                break
            except PlaywrightTimeout as error:
                note = timeout_note(error)
            except Exception as error:
                if type(error).__name__ == "SessionError":
                    raise
                note = f"검증 중 오류: {type(error).__name__}: {str(error).splitlines()[0] if str(error) else ''}"
            logging.warning("[LANDING] %s %s %s 시도 %d/%d 실패 - %s", target.site, division, ps.set_no, attempt,
                            SET_ATTEMPTS, note)
            set_result = set_rows(target, PAGE_LANDING, ps, RESULT_CHECK, f"{note} (Set {SET_ATTEMPTS}회 시도)")
        used.update(r["SKU"] for r in set_result if r.get("SKU"))
        rows += set_result
    return rows


if __name__ == "__main__":
    entry(PAGE_LANDING, check_division, LANDING_OUTPUT, "Landing Page Key Spec Checker")
