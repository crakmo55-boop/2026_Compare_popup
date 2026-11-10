# [결함 보고 초안] DE 냉동고 3종 – New Compare(PF Popup / Landing)에서 Key Specs 섹션 미노출

- 작성: 2026-11-10 (자동화 결과 기반 초안, 수동 확인 전)
- 환경: p6-pre-qa3 (`https://p6-pre-qa3.samsung.com/de/`)
- 영역: New Compare (`pdd43-master-compare`) – PF Compare 팝업, Compare Landing 페이지
- 심각도(제안): Medium – 비교 화면에서 Key Specs 비교 정보가 전부 빠짐

## 1. 대상 제품
| SKU | 제품명 (매핑 FMY_MKT_NM) |
|---|---|
| RZ70H32HDTEF | RZ75 Eintür-Gefriergerät, 186 cm, SpaceMax™ mit AI Energy Mode |
| RZ80H32HDTEF | RZ80 Eintür-Gefriergerät, 186 cm, SpaceMax™ mit AI Energy Mode |
| RZ70H32KDTEF | RZ70 Eintür-Gefriergerät, 186 cm, SpaceMax™ mit AI Energy Mode |

## 2. 현상
위 3개 제품을 함께 비교하면 Compare 화면에 **Key Specs 섹션(`.pdd43-master-compare__feature`)이 표시되지 않음.**
제품명·SKU 카드는 정상 표시되고, 화면 SKU는 매핑 SKU와 일치함 (다른 제품을 본 것이 아님).

## 3. 근거 (자동화 실행 2회, 서로 다른 페이지에서 동일 결과)
| 실행 | 페이지 | 결과 | 비고 |
|---|---|---|---|
| 2026-11-09 16:31 (`2026_KeySpec_Popup_20261109_163143.xlsx`) | PF Popup `/de/refrigerators/all-refrigerators/` | 3개 모두 "Key Specs 섹션 미노출" | Compare API `master/compare` HTTP 200, SKU 매칭 PASS ×3 |
| 2026-11-09 17:13 (`2026_KeySpec_Landing_20261109_171309.xlsx`) | Landing `/de/refrigerators/compare/` | 3개 모두 FAIL (섹션 미노출) | HTTP 200, SKU 매칭 PASS ×3 |

- 같은 실행에서 DE의 다른 Set(스마트폰·태블릿·TV·세탁기)은 Key Specs 정상 노출 → 환경·로그인 문제 아님.
- 2026-11-10 09:41 Popup 실행도 같은 결과였으나, 이 실행은 자동화 읽기 시점 문제(수정 완료)가 있었으므로 근거에서 제외함.

## 4. 재현 절차 (수동)
1. WMC 로그인 → QA(p6-preqa3) 이동
2. `https://p6-pre-qa3.samsung.com/de/refrigerators/all-refrigerators/` 접속
3. 위 3개 제품의 Compare 체크박스 선택 → Compare 버튼
4. Compare 팝업에서 Key Specs 영역 확인
5. (Landing) `https://p6-pre-qa3.samsung.com/de/refrigerators/compare/` 에서 열 1·2·3에 같은 제품 선택 후 Key Specs 영역 확인

- 기대 결과: Key Specs 섹션이 노출되고 제품별 값이 표시됨
- 실제 결과: Key Specs 섹션 미노출

## 5. 원인 추정 / 추가 확인 필요
- Compare 규칙상 **세트 중 Key Specs 데이터가 없는 제품이 1개라도 있으면 섹션 전체가 숨겨짐.**
  → 3개 중 어떤 제품(또는 전부)의 Key Specs 데이터가 비어 있는지 **제품별 단독/조합 확인 필요**
  (예: RZ70H32HDTEF + 다른 정상 냉장고 2개로 비교).
- 3개 모두 186 cm 1도어 냉동고(SpaceMax AI Energy Mode) 계열 → 같은 계열 데이터 누락 가능성.

## 6. 자동화 참고
- 자동화 판정: Popup = "확인 필요"(규칙상 원인 제품 특정 불가), Landing = "FAIL"
- 제출 전 위 4·5번 수동 확인 결과를 반영해 주세요.
