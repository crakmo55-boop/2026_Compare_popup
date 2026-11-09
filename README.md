# Key spec check (Jake_compare_key_spec)

Compare **Landing Page**와 **PF POPUP Page**에서 사업부(MX / VD / DA)별 Product Set의 Key Specs 노출을 검사합니다.
환경(URL·로그인·세션·결과 서식)은 `New_GNB_Code_Update`와 같은 방식이고, Key Specs 판정은 기존 Compare-main 코드 규칙을 따릅니다.

## 구조

```text
Jake_compare_key_spec/
├─ landing_checker.py      # Entry Point 1: Landing Page
├─ popup_checker.py        # Entry Point 2: PF POPUP Page
├─ common/
│  ├─ config.py            # 경로·환경·대기·CATEGORY_SPECS·PRODUCT_SET_RULE
│  ├─ targets.py           # base_urls.xlsx → 국가/환경 (LIVE / P6_PRE_QA3)
│  ├─ mapping.py           # All key spec Check.xlsx 로딩, Product Set 선정·검증
│  ├─ session.py           # 브라우저, WDS 로그인, 세션 만료 감지·재획득, 로그 마스킹
│  ├─ keyspec_page.py      # Compare 컴포넌트 Key Specs 읽기 (기존 JS 판정)
│  ├─ result.py            # 결과 행 + 단일 Excel Writer (GNB Quick 서식)
│  └─ runner.py            # 두 Checker 공통 실행 흐름
├─ login/
│  ├─ WDS_login_FS_Auto.py # GNB와 동일한 WDS 로그인 모듈
│  └─ .env                 # LOGIN_ID_Auto / LOGIN_PW_Auto (공유·커밋 금지)
├─ Docs/All key spec Check.xlsx
├─ URL/base_urls.xlsx      # 첫 시트 base_url 열
├─ Result/                 # 2026_KeySpec_Landing.xlsx / 2026_KeySpec_Popup.xlsx
└─ Scratch/                # Debug 스크립트 (debug_bootstrap.py)
```

## 실행

```powershell
pip install openpyxl playwright python-dotenv
playwright install chromium

python landing_checker.py --dry-run   # 대상·환경·로그인 여부·Product Set 확인만
python landing_checker.py
python popup_checker.py
python Scratch/debug_bootstrap.py      # 공통 환경 Debug (실제 로그인 없음)
```

- `base_urls.xlsx`에 적힌 국가만 실행합니다. `www.samsung.com`은 로그인을 생략하고, `p6-pre-qa3`는 WDS에 로그인합니다.
- 빈 URL, 잘못된 URL, 지원하지 않는 환경, 중복 국가가 있으면 실행하지 않습니다.
- 제품 선정 규칙: `PRODUCT_SET_RULE = "category_fmy_id"` (`common/mapping.py`의 `rule_category_fmy_id`, 모든 국가 / Landing·Popup 공통)
  - MX: Latest = Smartphones, Middle = Tablets
  - VD: Latest = TVs, Middle = TVs
  - DA: Latest = Refrigerators, Middle = Washers & Dryers (Laundry)
  - 카테고리는 IA_CD 앞 4자리로 구분합니다 (`config.SET_CATEGORIES`, `config.PAGE_IA_PREFIX`). 그 카테고리의 PF / Compare 페이지만 엽니다.
  - Latest = 카테고리 안에서 FMY_ID가 가장 큰 3개, Middle = 나머지 제품 중 가운데 3개 (카테고리가 다르면 카테고리 전체의 가운데 3개)
  - 같은 입력이면 항상 같은 Set이 나옵니다. 3개가 안 되면 '확인 필요'입니다.
  - 화면(PF 카드 / Landing 드롭다운)에 없는 제품은 같은 규칙의 다음 후보로 바꿔 다시 찾습니다 (`config.MAX_ALTERNATES`, 기본 10개).
    Latest는 그 다음으로 FMY_ID가 큰 제품, Middle은 가운데에 가까운 제품 순서이며, 같은 사업부의 다른 Set 제품과 겹치지 않습니다.
    대체된 행은 비고 앞에 `[대체] 원래 SKU (제품명) 화면에 없음 → 다음 후보 선택`이 붙습니다.
- 결과 파일: 실행할 때마다 새 파일 `Result/2026_KeySpec_Landing_YYYYMMDD_HHMMSS.xlsx` / `2026_KeySpec_Popup_YYYYMMDD_HHMMSS.xlsx`
  - 열: 국가, 사업부, Set, 구분, mapping table 텍스트, 최종 페이지 랜딩 후 제품 텍스트, SKU, 최종 페이지 SKU, SKU 매칭 결과, HTTP 상태, Key spec 존재 결과, 비고
  - SKU 매칭 결과: mapping SKU = 최종 페이지 SKU → PASS, 다르면 FAIL, 최종 페이지까지 가지 못하면 '확인 필요'
- 국가 병렬 / 세션 재사용: 국가를 `COUNTRY_PARALLEL`(기본 3)개씩 동시에 처리하고(`--parallel N`, 1이면 순서대로),
  WDS 로그인은 환경별로 1번만 한 뒤 모든 국가가 그 세션(쿠키)을 복사해 씁니다 (CN은 goLink가 달라 별도 1회).
  세션 만료는 국가·사업부가 동시에 만나도 재획득을 1번만 합니다. 결과 파일은 국가가 끝날 때마다 base_urls 순서로 다시 저장됩니다.
  가상 실행(실제 접속 없음): `python Scratch/sim_parallel_run.py [국가 수] [slow]`
- Landing 제품 선택: Compare 페이지 열마다 제품 드롭다운에서 이름이 비슷한 옵션을 점수 순으로 시도하고(같은 이름 옵션 여러 개 대비),
  열에 표시된 SKU가 매핑 SKU와 같아질 때까지 용량/사이즈 드롭다운도 맞춥니다. 끝내 SKU가 맞지 않으면 같은 열에 매핑 대체 후보를 시도하고,
  그래도 안 되면 '확인 필요'입니다 (다른 제품이 표시된 상태로는 Key spec을 판정하지 않음). Landing은 PF를 거치지 않으므로 View more가 없습니다.
- WMC 로그인 후 뜨는 공지 팝업(`wmcNotice`)은 goLink 클릭 전에 '닫기'로 닫고, 남은 공지는 화면에서 숨깁니다.
- 새 Compare 컴포넌트가 없는 페이지(Live 현재 상태)는 Key spec을 판정하지 않고 '확인 필요'로 기록합니다.
