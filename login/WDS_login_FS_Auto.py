# 삼성 WDS FS 로그인 모듈
import asyncio
import os
import logging
from dotenv import load_dotenv
from playwright.async_api import Page, BrowserContext

# ------------------------
# 환경 변수 로드
# ------------------------
load_dotenv()
LOGIN_ID = os.getenv("LOGIN_ID_Auto")
LOGIN_PW = os.getenv("LOGIN_PW_Auto")

async def stub_adfs_ajax_call(page: Page) -> None:
    await page.evaluate("() => { window.jsADFSAjaxCall = () => {}; }")


# ------------------------
# 로그인 함수
# ------------------------
async def samsung_login_and_move(page: Page, country_code: str = None) -> bool:
    return await samsung_login(page, country_code=country_code)


async def samsung_login(page: Page, login_id: str = None, login_pw: str = None, country_code: str = None) -> bool:

    try:
        # 로그인 정보 설정
        user_id = login_id if login_id else LOGIN_ID
        user_pw = login_pw if login_pw else LOGIN_PW
        
        if not user_id or not user_pw:
            logging.error("로그인 정보가 없습니다.")
            return False
        
        # 로그인 페이지 이동
        await page.goto("https://wds.samsung.com/wds/sso/login/forwardLogin.do")
        
        # SSO 로그인 버튼 클릭
        async with page.context.expect_page() as new_page_info:
            await page.click("a[href='javascript:SSO0101_V.sslChkAction();']")
        
        # 새 창이 열릴 때까지 대기
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded")
        await stub_adfs_ajax_call(new_page)

        # 새 창에 로그인 정보 입력
        await new_page.fill("input[name='UserName']", user_id)
        await new_page.fill("input[name='Password']", user_pw)
        
        # 새 창에서 로그인 버튼 클릭
        await new_page.click("span#submitButton")
        
        # 로그인 완료 대기 (국가 코드에 따라 다른 링크 대기)
        if country_code and country_code.lower() == 'cn':
            await page.wait_for_selector(
                "a[href=\"javascript:goLink('p6-preqa3-cn');\"]:visible",
                timeout=30000
            )
        else:
            await page.wait_for_selector(
                "a[href=\"javascript:goLink('p6-preqa3');\"]:visible",
                timeout=30000
            )
        
        return True
        
    except Exception:
        logging.exception("WDS 로그인 중 오류 발생")
        return False


async def move_to_FS(context: BrowserContext, country_code: str = None) -> None:

    wds_page = context.pages[0]
    
    # 중국은 Preqa2 CN, 일반 국가는 Preqa2 링크 사용
    if country_code and country_code.lower() == 'cn':
        await wds_page.locator(
            "a[href=\"javascript:goLink('p6-preqa3-cn');\"]:visible"
        ).click()
    else:
        await wds_page.locator(
            "a[href=\"javascript:goLink('p6-preqa3');\"]:visible"
        ).click()
    
    await asyncio.sleep(2)
    page = context.pages[-1]
    await page.wait_for_load_state("networkidle")
