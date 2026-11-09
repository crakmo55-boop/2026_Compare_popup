# -*- coding: utf-8 -*-
"""[Entry Point 1] Landing Page (Compare models 페이지) Key Spec Checker.

공통 흐름(common/runner.py): base_urls → 국가/환경 → 로그인(p6-pre-qa3)/생략(Live) → 매핑 → Product Set
이 파일: 사업부(MX/VD/DA)마다 병렬로
  Compare 페이지 열기 → 열 1·2·3 드롭다운에서 Set 의 제품 선택 → 제품별 Key Specs 확인

판정은 기존 run_c_landing_live.py 와 같음
  - 섹션 노출 + 해당 열 값 있음 → PASS / 값 없음 → FAIL / 섹션 미노출 → FAIL
HTTP 상태 = Compare 페이지 이동(메인 문서) 응답 코드.
CTA(Buy / Learn more)는 누르지 않는다 (Key spec 존재 확인만).

실행: python landing_checker.py [--dry-run] [--retries N]
"""
import dataclasses
import logging
import re

from playwright.async_api import TimeoutError as PlaywrightTimeout

from common.config import (CATEGORY_SPECS, DIVISION_PAGES, LANDING_OUTPUT, MAX_ALTERNATES, PAGE_LANDING, RESULT_CHECK,
                           RESULT_FAIL, SET_SIZE, TIMEOUTS)
from common.keyspec_page import dismiss_cookie_banner, keyspec_result, timeout_note, norm, read_state
from common.result import make_row, replaced_note, set_rows
from common.runner import entry

ROOT = ".pdd43-master-compare"
CARD = "li.pdd43-master-compare__card-item.pdd43-master-compare__column--{n}"

# 실패 진단: Compare 영역 / 카드 / 드롭다운 구조 요약 (비고·로그 기록용, 실제 p6 구조 확인 목적)
JS_DIAG = r"""() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('.pdd43-master-compare');
  const cards = root ? [...root.querySelectorAll('li.pdd43-master-compare__card-item')] : [];
  const cmpLike = [...new Set([...document.querySelectorAll('[class*="compare" i]')]
      .map(e => String(e.className).split(/\s+/).find(c => /compare/i.test(c))).filter(Boolean))].slice(0, 8);
  return {title: document.title.slice(0, 60), url: location.pathname, root: !!root, cards: cards.length,
          cardCols: cards.map(c => (String(c.className).match(/column--(\d+)/) || [])[1]).join(','),
          skus: [...document.querySelectorAll('.pdd43-master-compare__sku-text')].map(e => clean(e.innerText)).slice(0, 4),
          selects: document.querySelectorAll('.pdd43-master-compare button.menu__select-field').length,
          options: document.querySelectorAll('.pdd43-master-compare .menu__list-option').length,
          compareClasses: cmpLike};
}"""


async def diag(page) -> str:
    try:
        d = await page.evaluate(JS_DIAG)
    except Exception as error:
        return f"진단 실패 {type(error).__name__}"
    try:
        dds = await page.evaluate(JS_DROPDOWNS, 1) or []
        dd_note = "; ".join(f"드롭다운{dd['d']} '{dd['field'][:30]}' 옵션 {len(dd['options'])}개 "
                            f"{[o['text'][:30] for o in dd['options'][:3]]}" for dd in dds)
    except Exception as error:
        dd_note = f"드롭다운 읽기 실패 {type(error).__name__}"
    return (f"진단: 제목 '{d['title']}', Compare 루트 {'O' if d['root'] else 'X'}, 카드 {d['cards']}개(열 {d['cardCols']}), "
            f"SKU {d['skus']}, 드롭다운 {d['selects']}개, 옵션 {d['options']}개 | 1열 {dd_note}")


def options_sample(options, n=6) -> str:
    """드롭다운 옵션 예시: 텍스트 + 짧은 속성값 (모델코드가 어디 있는지 확인용)."""
    out = []
    for o in (options or [])[:n]:
        vals = [str(v) for v in o.get("values", []) if v and len(str(v)) <= 30][:4]
        out.append(f"'{o.get('text', '')[:40]}' {vals}")
    return f"옵션 {len(options or [])}개 예: " + "; ".join(out)


def landing_candidates(ps):
    """Set 카테고리(ps.page_key)가 정해져 있으면 그 Compare 페이지 하나만, 아니면 사업부 페이지 전체."""
    keys = (ps.page_key,) if ps.page_key else DIVISION_PAGES[ps.division]
    return [spec for spec in CATEGORY_SPECS if spec[1] in keys]


# 열 N 카드의 드롭다운 목록 (2026-11-09 p6 실측: 카드마다 드롭다운 2개, 옵션은 닫힌 상태라 innerText 가 '')
#   - 텍스트는 textContent 로 읽는다 (닫힌 드롭다운도 읽힘)
#   - 드롭다운마다 옵션을 따로 돌려준다 (예: 0 = 제품, 1 = 용량/사이즈/색상)
#   - 나중에 클릭할 수 있도록 data-ks-* 표시를 붙인다 (인덱스가 다시 그려져도 같은 요소를 가리키도록)
JS_DROPDOWNS = r"""(n) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const card = document.querySelector(`.pdd43-master-compare li.pdd43-master-compare__card-item.pdd43-master-compare__column--${n}`);
  if (!card) return null;
  const fields = [...card.querySelectorAll('button.menu__select-field')];
  return fields.map((f, d) => {
    const menu = f.closest('.menu') || f.parentElement;
    f.setAttribute('data-ks-field', `${n}-${d}`);
    const opts = [...menu.querySelectorAll('.menu__list-option')];
    return {d, field: clean(f.textContent).slice(0, 80), expanded: f.getAttribute('aria-expanded'),
            options: opts.map((o, i) => {
              o.setAttribute('data-ks-opt', `${n}-${d}-${i}`);
              return {i, d, text: clean(o.textContent), selected: o.getAttribute('aria-selected') === 'true',
                      values: [...o.attributes].map(a => a.value)
                               .concat([...o.querySelectorAll('*')].flatMap(e => [...e.attributes].map(a => a.value)))};
            })};
  });
}"""

JS_COLUMN_SKU = r"""(n) => {
  const card = document.querySelector(`.pdd43-master-compare li.pdd43-master-compare__card-item.pdd43-master-compare__column--${n}`);
  const e = card && card.querySelector('.pdd43-master-compare__sku-text');
  return e ? e.textContent.trim() : null;
}"""


_STOP = {"samsung", "vision", "ai", "smart", "tv", "the", "with", "and", "de", "con", "com", "da", "do", "e", "y", "et",
         "inch", "size", "new", "only"}
_SIZE_PLACEHOLDER = re.compile(r"\{\s*(size|inch)\s*\}", re.I)


def _size_from_sku(code) -> str:
    """TV 계열 SKU 의 화면 크기 (UE65U8000HKXXU → 65, MRE115MR95FXXU → 115). 없으면 ''."""
    m = re.match(r"^[A-Z]{2,3}(\d{2,3})[A-Z]", code or "")
    return m.group(1) if m else ""


def _tokens(text):
    text = (text or "").replace("™", " ").replace("®", " ").replace("​", " ")
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP]


def _weight(tok) -> float:
    if re.search(r"[a-z]", tok) and re.search(r"\d", tok):
        return 3.0          # 모델 코드성 토큰 (u7000h, qn990h, s11, 11kg)
    if tok.isdigit():
        return 2.0          # 크기·용량 숫자 (65, 323)
    return 1.0


def option_score(option_text, product) -> float:
    """드롭다운 옵션 텍스트와 매핑 제품명의 유사도 (0~1). 가중 토큰 F1.
    매핑 제품명의 {size}/{inch} 는 SKU 의 크기로 바꾸고, TV 는 SKU 크기를 토큰으로 더한다."""
    size = _size_from_sku(product.rep_model)
    best = 0.0
    opt = _tokens(option_text)
    if not opt:
        return 0.0
    for name in {product.fmy_mkt, product.fmy_eng}:
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


MIN_SCORE = 0.45


def rank_options(options, product):
    """제품에 맞을 법한 옵션 순서. 속성값에 SKU 가 있으면 맨 앞. 점수 MIN_SCORE 미만은 제외."""
    code = product.rep_model.lower()
    exact = [o for o in options if any(str(v).strip().lower() == code for v in o["values"])]
    scored = sorted(((option_score(o["text"], product), o) for o in options if o not in exact),
                    key=lambda t: -t[0])
    return exact + [o for s, o in scored if s >= MIN_SCORE]


def match_option(options, product):
    """(호환용) 가장 그럴듯한 옵션 1개."""
    ranked = rank_options(options, product)
    return (ranked[0], "제품명 유사") if ranked else (None, "")


async def read_dropdowns(page, n, attempts=2):
    """열 n 의 드롭다운들. 옵션이 비어 있으면(늦게 그려지는 경우) 열어서 다시 읽고 닫는다."""
    dds = []
    for _ in range(attempts):
        dds = await page.evaluate(JS_DROPDOWNS, n) or []
        if dds and all(dd["options"] for dd in dds[:1]):
            return dds
        for dd in dds:
            if dd["options"]:
                continue
            try:
                await page.locator(f'[data-ks-field="{n}-{dd["d"]}"]').click(timeout=TIMEOUTS["click"])
                await page.wait_for_function(
                    "(sel) => document.querySelectorAll(sel).length > 0",
                    arg=f'{CARD.format(n=n)} .menu__list-option', timeout=5000)
            except PlaywrightTimeout:
                pass
            await page.keyboard.press("Escape")
    return dds


async def read_options(page, n):
    """열 n 의 첫 번째(제품) 드롭다운 옵션."""
    dds = await read_dropdowns(page, n)
    return dds[0]["options"] if dds else []


async def _choose(page, n, option):
    """드롭다운 option 을 선택하고 열 n 의 SKU 가 바뀔 때까지 기다린다.
    aria-selected 는 믿지 않는다 (2026-11-09 ES: 선택 표시는 true 인데 열은 다른 제품) → 항상 클릭.
    화면 클릭이 반영되지 않으면 DOM click 으로 한 번 더."""
    before = await page.evaluate(JS_COLUMN_SKU, n)
    field = page.locator(f'[data-ks-field="{n}-{option["d"]}"]')
    opt = page.locator(f'[data-ks-opt="{n}-{option["d"]}-{option["i"]}"]')
    for how in ("click", "dom"):
        try:
            if how == "click":
                if await field.get_attribute("aria-expanded") != "true":
                    await field.click(timeout=TIMEOUTS["click"])
                await opt.scroll_into_view_if_needed(timeout=TIMEOUTS["click"])
                await opt.click(timeout=TIMEOUTS["click"])
            else:
                await opt.evaluate("e => e.click()")
            await page.wait_for_function(f"([n, before]) => ({JS_COLUMN_SKU})(n) !== before", arg=[n, before],
                                         timeout=TIMEOUTS["column_change"] if how == "dom" else 8000)
            break
        except PlaywrightTimeout:
            continue                     # 반영 안 됨 → DOM click 재시도 / 이미 그 제품(같은 SKU)일 수도 있음
    await page.keyboard.press("Escape")


async def _sku(page, n) -> str:
    return (await page.evaluate(JS_COLUMN_SKU, n) or "").strip().upper()


async def select_product(page, n, product, top_k=4, max_variant_tries=8):
    """열 n 에 product 를 선택하고 열 SKU 가 REP_MODEL 과 같아지면 성공.
    1) 제품 드롭다운에서 이름이 비슷한 옵션을 점수 순으로 최대 top_k 개 시도 (같은 이름 옵션이 여러 개인 경우 대비)
    2) 각 옵션에서 SKU 가 다르면 나머지 드롭다운(용량/사이즈/색상)에서 SKU 가 맞는 옵션을 찾는다
    반환: (성공 여부, 설명, 선택한 옵션 텍스트)"""
    code = product.rep_model.strip().upper()
    if await _sku(page, n) == code:
        return True, "이미 선택됨", ""
    dds = await read_dropdowns(page, n)
    if not dds or not dds[0]["options"]:
        return False, f"{n}열 드롭다운 옵션 없음", ""
    ranked = rank_options(dds[0]["options"], product)[:top_k]
    if not ranked:
        return False, f"{n}열 제품 드롭다운에 '{product.fmy_mkt}' 와 비슷한 옵션 없음", ""
    tried = []
    for option in ranked:
        await _choose(page, n, option)
        sku = await _sku(page, n)
        tried.append(f"'{option['text'][:30]}'→{sku or '-'}")
        if sku == code:
            return True, f"제품 옵션 '{option['text'][:40]}'", option["text"]
        for dd in (await read_dropdowns(page, n))[1:]:
            direct = next((o for o in dd["options"] if any(str(v).strip().upper() == code for v in o["values"])), None)
            for cand in ([direct] if direct else dd["options"][:max_variant_tries]):
                await _choose(page, n, cand)
                if await _sku(page, n) == code:
                    return True, f"제품 옵션 '{option['text'][:40]}' + '{cand['text'][:20]}'", option["text"]
                fresh = await read_dropdowns(page, n)            # 다시 그려졌을 수 있음 → 표시 다시 붙임
                if len(fresh) <= dd["d"]:
                    break
    return False, f"{n}열 SKU {code} 를 맞추지 못함 (시도 {'; '.join(tried)})", ""


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
        if not await page.locator(ROOT).count():
            tried[-1] += f"(새 Compare 영역 없음 | {await diag(page)})"
            continue
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('.pdd43-master-compare .pdd43-master-compare__sku-text').length > 0",
                timeout=TIMEOUTS["element"])
        except PlaywrightTimeout:
            tried[-1] += f"(Compare 제품 카드 미표시 | {await diag(page)})"
            continue
        yield url, status


async def check_set(session, ctx, target, ps, used=()):
    """Compare 페이지에서 열 1·2·3 에 Set 제품을 고르고 Key Specs 를 확인한다.
    제품을 고르지 못하면(드롭다운에 없음 / SKU 를 맞추지 못함) 매핑 대체 후보를 순서대로 같은 열에 시도한다."""
    page = await ctx.new_page()
    try:
        tried = []
        async for url, status in open_compare_page(session, page, target, ps, tried):
            await dismiss_cookie_banner(page)
            options = await read_options(page, 1) or []
            pool = [p for p in ps.alternates if p.rep_model not in used][:MAX_ALTERNATES]
            chosen, replaced, fails = [], {}, {}
            for n, orig in enumerate(ps.products[:SET_SIZE], start=1):
                picked = None
                for cand in [orig] + pool:
                    if cand.rep_model in {c.rep_model for c, _ in chosen}:
                        continue
                    if not rank_options(options, cand):          # 드롭다운에 비슷한 이름도 없음 → 바로 다음 후보
                        if cand is orig:
                            fails[n] = f"제품 드롭다운에 '{orig.fmy_mkt}' 와 비슷한 옵션 없음"
                        continue
                    ok, how, _ = await select_product(page, n, cand)
                    if ok:
                        picked = (cand, how)
                        break
                    if cand is orig:
                        fails[n] = how
                if picked:
                    if picked[0] is not orig:
                        replaced[picked[0].rep_model] = orig
                        pool = [p for p in pool if p is not picked[0]]
                    chosen.append(picked)
                else:
                    chosen.append((orig, None))
            state, columns = await read_state(page, ROOT)
            by_col = {c["col"]: c for c in columns}
            final_ps = dataclasses.replace(ps, products=[c for c, _ in chosen])
            rows = []
            for n, (product, how) in enumerate(chosen, start=1):
                col = by_col.get(n, {})
                sku_ok = (col.get("sku") or "").strip().upper() == product.rep_model.upper()
                prefix = replaced_note(product, replaced)
                if how is None or not sku_ok:
                    reason = fails.get(n, "선택한 열의 SKU 가 다름")
                    if how is None and pool is not None:
                        reason += f" | 대체 후보 {len(ps.alternates[:MAX_ALTERNATES])}개도 찾지 못함"
                    rows.append(make_row(target, PAGE_LANDING, final_ps, product, final_text=col.get("name", ""),
                                         final_sku=col.get("sku", ""), http=status, result=RESULT_CHECK,
                                         note=prefix + f"{reason} - 다른 제품이 표시된 상태라 Key spec 판정 안 함 | {url}"))
                    continue
                result, detail = keyspec_result(state, n, RESULT_FAIL, "Key Specs 섹션 미노출")
                rows.append(make_row(target, PAGE_LANDING, final_ps, product, final_text=col.get("name", ""),
                                     final_sku=col.get("sku", ""), http=status, result=result,
                                     note=prefix + f"{n}열 {detail} ({how}) | {url}"))
            if replaced:
                logging.info("[LANDING] %s %s %s 대체 %s", target.site, ps.division, ps.set_no,
                             {o.rep_model: s for s, o in replaced.items()})
            logging.info("[LANDING] %s %s %s → %s", target.site, ps.division, ps.set_no,
                         [r["Key spec 존재 결과"] for r in rows])
            bad = [r for r in rows if r["Key spec 존재 결과"] == RESULT_CHECK]
            if bad:
                logging.warning("[LANDING] %s %s %s 확인 필요 %d건 - %s | %s | %s", target.site, ps.division, ps.set_no,
                                len(bad), bad[0]["비고"][:300], options_sample(options), await diag(page))
            return rows
        note = "Compare 페이지를 열지 못함 (시도: " + ", ".join(tried) + ")"
        logging.warning("[LANDING] %s %s %s 확인 필요 - %s", target.site, ps.division, ps.set_no, note)
        return set_rows(target, PAGE_LANDING, ps, RESULT_CHECK, note)
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
                logging.warning("[LANDING] %s %s %s 시간 초과 - %s", target.site, division, ps.set_no, timeout_note(error))
                set_result = set_rows(target, PAGE_LANDING, ps, RESULT_CHECK, timeout_note(error))
            used.update(r["SKU"] for r in set_result if r.get("SKU"))
            rows += set_result
    return rows


if __name__ == "__main__":
    entry(PAGE_LANDING, check_division, LANDING_OUTPUT, "Landing Page Key Spec Checker")
