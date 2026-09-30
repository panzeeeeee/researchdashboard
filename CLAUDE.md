# 모닝 브리프 (구 리서치 콘솔) — 프로젝트 안내

이 문서는 새 대화를 시작할 때 Claude가 먼저 읽는 배경 설명입니다.
대시보드가 무엇이고, 어떤 구조이며, 왜 그렇게 정해졌는지를 담았습니다.
마지막 정리: 2026-09-30 (Claude Code 전환 직전)

## 한 줄 요약

매일 아침·저녁 스크리너가 미국 주식을 걸러내고, 그 종목을 기준으로
뉴스·리포트·실적 공시를 모으고, 시장·금리·유동성·실적 흐름까지
한 페이지에 보여주는 개인용 대시보드. 아침 텔레그램 요약이 1차 읽기 자료.

주소: `https://panzeeeeee.github.io/researchdashboard/`
저장소: `panzeeeeee/researchdashboard` (Private)

## 구조

서버가 없습니다. GitHub Actions가 정해진 시각에 스크립트를 돌려
JSON을 만들고, GitHub Pages가 그 JSON을 화면으로 보여줍니다.
JH의 컴퓨터가 꺼져 있어도 돌아갑니다.

워크플로우는 두 개입니다. 둘은 같은 줄(concurrency group: news)에 서서
동시에 커밋하다 부딪히지 않습니다.

| 파일 | 이름 | 시각(한국) | 하는 일 |
|---|---|---|---|
| `.github/workflows/news.yml` | 무거운 본체 | 평일 8:00, 18:30 | 스크리너·뉴스·공시·포트폴리오·텔레그램 |
| `.github/workflows/amazon.yml` | 아마존·수출입·배너 | 매일 7:30, 18:00 | 가벼운 수집. `docs/data` 폴더 통째로 커밋 |

**가벼운 수집은 앞으로도 amazon.yml에 붙인다.** 새 화면용 파일이 생겨도
커밋 단계를 고칠 필요가 없다.

amazon.yml 단계: fetch_banner → fetch_amazon → fetch_trade → fetch_earnings

핵심 파일은 `picks.json`입니다. 스크리너 결과가 여기로 모이고,
뒤쪽 수집기들이 이 파일을 읽습니다.

## 화면 구성

사이드바로 화면을 전환합니다(스크롤이 아니라 화면 교체).
사이드바는 5묶음, 묶음마다 카드 + 고유 색. 화면 제목 앞 색 띠도 묶음 색.
전체 글씨 18px 기준, 휴대폰 레이아웃 대응. 머리말 배너 표시는 뺐음(데이터는 계속 생성).

| 묶음 | 화면 |
|---|---|
| 오늘 | 오늘의 종목, 주요뉴스, 섹터 뉴스, 주요 일정 |
| 시장 | 미국 시황, 섹터 온도, 원자재·귀금속, 금리·환율, 경제 지표 |
| 돈의 흐름 | 글로벌 유동성, 크레딧 스프레드, 자금시장 |
| 스크리너 | 긴 조정 후 신고가, 딥밸류, 신고가·신저가, **미국 실적 레이더** |
| 내 종목·리서치 | 포트폴리오, 컨콜 흐름, 센텔리안24 아마존 |

**한국 탭(`kr.html`)** 도 같은 사이드바 방식(`s-` 접두사 섹션, 해시로 전환). 머리말의 미국/한국 탭으로 오간다.

| 묶음 | 화면 |
|---|---|
| 오늘 | 주요 뉴스, 공시 |
| 시장 | 지수·업종, 국고채 금리 |
| 리서치 | 수출입 레이더 (2026-09-30 미국 탭에서 한국 탭으로 옮김) |

- 금리·환율: 미국채 3M~30Y, 10Y-2Y, 원/달러·엔/달러·원/100엔·DXY. 실시간 아님
- 수출입 레이더: `trade.html` 별도 페이지(관세청 API, 14개 테마). 한국 탭 안에 iframe으로 보임
- 센텔리안24 아마존: 구글 시트의 "웹에 게시" CSV를 하루 1회 읽음
- 화면 배치 원칙: 차트가 있으면 차트를 카드·표보다 맨 위에

## 미국 실적 레이더 (2026-09-30 완성, 1~4단계)

목적: 스크리너 종목만이 아니라 미국 실적 발표 전체를 훑어, 어떤 섹터·산업이
좋아지고 나빠지는지 판단. 한국 공시봇 같은 방식이되 하루 두 번 모아 보면 충분.

`scripts/fetch_earnings.py` → `docs/data/earnings.json` (90일 보관)

| 단계 | 방식 |
|---|---|
| 수집 | SEC "최신 공시" Atom 피드에서 8-K 중 Item 2.02만. 최근 40시간. 정정(8-K/A) 제외 |
| 티커 | `fetch_edgar.py`의 CIK 대응표(`screener/ticker_cik.json`) 재사용 |
| 주가 반응 | 야후 일괄 요청. 장전·장중 = 전날→발표일 종가, 장후 = 발표일→다음 거래일 종가(그 전엔 "대기"). SPY 빼서 시장 대비 %p |
| 숫자 추출 | 보도자료(EX-99)를 `fetch_edgar.press_release`로 찾아 Gemini가 분기·매출·전년 매출·GAAP/조정 EPS와 전년치·가이던스(상향/유지/하향/첫 제시/없음)·한국어 한 줄 요약을 JSON으로. YoY는 코드가 계산 |
| 한도 | 실행당 20건, 호출 사이 5초. 큰 회사부터(SEC 티커 목록 순서 ≈ 시총 순). 2번 실패하면 포기 |
| 화면 | 날짜 버튼, 요약 카드 4개(발표 수·시장보다 오른 곳·가이던스↑↓·매출 YoY 중앙값), 정렬(발표 순/강한 순/약한 순), 표. 원문 링크 |

아침 7:30 실행 한 번에 전날 장전·장후 발표가 다 잡힌다(장후 발표는 한국 새벽 5~6시).
amazon.yml 단계에 `SEC_USER_AGENT`, `GEMINI_API_KEY` 연결됨.

## 텔레그램 요약 (push_digest.py)

- 아침에만. 종목별 matplotlib 차트 첨부 — 차트 보고 종목을 거른 뒤 대시보드에서 최종 판단
- **52주 신저가는 종목별로 보내지 않는다.** 스크리너 결과 첫 메시지에
  업종별 신저가 상위 3개(개수, 전체 중 %, 같은 업종 신고가 수)와 해석 한 줄만.
  업종별 전체 종목 수 데이터가 없어서 "업종 내 %"는 못 넣었음

## 스크립트

| 파일 | 하는 일 |
|---|---|
| `us_breakout.py` | 신고가·물극필반 스크리너 (JH가 직접 만든 것) |
| `deepvalue/dv_*.py` | 딥밸류 스크리너 (JH가 직접 만든 것) |
| `bridge_universe.py` | 신고가가 받은 종목 목록을 딥밸류에 넘김 |
| `make_picks.py` | 엑셀 → picks.json |
| `fetch_portfolio.py` | 보유 종목 시세, 트리거 거리 |
| `fetch_edgar.py` | SEC 8-K 실적 보도자료 원문 (오늘의 종목 8개) |
| `fetch_earnings.py` | 미국 실적 레이더 (미국 전체) |
| `fetch_calls.py` | 분기별 요약 (근거: 8-K 원문 + 컨콜 브리프) |
| `make_tables.py` | 스크리너 결과 표, 통과 추이 |
| `fetch_news.py` | 종목 뉴스 + 자동 섹터 + 고정 섹터 |
| `fetch_research.py` | 증권사 리포트 링크 |
| `fetch_banner.py` | 금리·유가·환율 |
| `fetch_amazon.py` | 센텔리안24 아마존 시트 CSV |
| `fetch_trade.py` | 관세청 수출입 통계 |
| `push_telegram.py` | 고정 섹터 뉴스 알림 |
| `push_digest.py` | 아침 텔레그램 요약 |
| `common.py` | 공용 유틸, Gemini 호출(`ask_gemini`) |

## Secrets

DATA_GO_KR_KEY(공공데이터포털, 계정당 1개), EQUIBLES_API_KEY, GEMINI_API_KEY,
SEC_USER_AGENT, TELEGRAM_DIGEST_BOT_TOKEN, TELEGRAM_DIGEST_CHAT_ID

## 설정 파일

- `config/sectors.yaml` — 고정 관심 업종과 검색어
- `config/watchlist.yaml` — 관심종목
- `config/portfolio.yaml` — 보유 종목과 트리거 단계

## 그동안 내린 결정과 이유

**네이버 검색은 NAVER API HUB로 쓴다.** 2026-07-31부터 개발자센터(developers.naver.com)의 검색 API 신규 신청이
막혔고 기존 키도 2027-06-30에 끊긴다. `fetch_news.py`의 `from_naver`는 HUB 주소
(`naverapihub.apigw.ntruss.com/search/v1/news`)와 헤더(`X-NCP-APIGW-API-KEY-ID`/`X-NCP-APIGW-API-KEY`)를 쓴다.
Secret 이름은 `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET` 그대로이고 값만 HUB 앱의 Client ID/Secret이다.
키가 없으면 구글 뉴스 RSS로 돌아간다. HUB는 지금 한시적 무료(검색 월 77.5만 건, 초당 50건).

**가격 캐시를 압축했다.** 원래 161MB라 GitHub에 못 올렸다.
종가·거래량만 남기고 float32 + gzip으로 22MB.

**딥밸류 러셀2000은 신고가의 종목 목록을 빌려 쓴다.** 신고가 쪽 파서가 형식 변화에 더 잘 견딘다.

**Gemini 모델 이름을 박아두지 않는다.** 구글이 자주 갈아치워서 두 번 연속 404가 났다.
API에 사용 가능 목록을 물어보고 고른다(flash 계열 최신 우선).

**야후 시세는 한 번에 받는다.** 종목마다 요청하면 429로 막힌다.

**SEC 실적은 최신 공시 피드로 받는다.** 일일 목록 파일은 미국 밤 10시 이후에 나와서
한국 아침 수집에 늦는다. 회사별로 묻지 않으니 요청 수십 번이면 끝난다.

**저작권.** 컨콜 전문과 Q&A, 증권사 리포트 PDF는 저장하지 않는다.
SEC 8-K는 미국 정부 공시라 원문 보관 가능.

**AI 요약은 검증되지 않은 것으로 취급한다.** "왜 싼가", 컨콜 요약, 뉴스 요약,
실적 숫자 추출 모두 자동 생성. 판단 근거가 아니라 실마리. 화면에 원문 링크를 같이 둔다.

## 센텔리안24 아마존

- 원본 구글 시트 이름은 "센텔리안24 아마존 트래커"로 고정. 새 파일을 만들지 않고 이 파일에 날짜 열만 추가
- 시트 업데이트(아마존 읽기)는 수동 — "업데이트" 요청 시 Claude가 날짜 열 추가. 09-19~09-29는 트래킹 공백
- 대시보드는 `config/amazon.yaml`의 `sheet_url`(웹에 게시 주소)을 읽음. 웹 페이지(pubhtml)·CSV 어느 형식이든
  스크립트가 CSV 주소로 바꿔 읽는다. 시트가 바뀌면 이 한 줄만 교체
- 이름으로 드라이브 검색하는 방식은 안 씀: Actions에는 JH 구글 로그인이 없어서 서비스 계정 설정이 필요하고,
  새 시트마다 공유도 해줘야 해서 손이 덜 가지 않음

## 다음에 할 일

(완료, 2026-09-30) 아마존 그래프: 3일 넘게 빈 구간은 실선을 끊고 가는 점선(데이터 유실)으로 표시, 오른쪽 날짜 잘림 수정.
실적 레이더 5단계: `push_digest.py`에 "밤사이 미국 실적" 텔레그램 블록(첫 발송은 다음 평일 아침에 확인 필요).
실적 레이더 6단계: 실적 화면에 [종목별 | 업종별 집계] 탭. 업종은 SEC 산업코드(SIC)를 `fetch_earnings.py`의
`SIC_RULES`로 11개 대분류에 묶은 것(GICS 아님). 종목마다 `sector`, `industry`, `sic`가 earnings.json에 붙는다.

(진행 중) 한국 탭 1단계: `scripts/fetch_kr_market.py` → `docs/data/kr_market.json` → `docs/kr.html`.
amazon.yml에 단계 추가, Secret `ECOS_API_KEY` 등록됨. 업종은 KODEX 업종 ETF로 대용(KRX 업종지수는 야후에 없음).
국고채는 ECOS 통계표 817Y002에서 항목 이름("국고채(3년)")으로 코드를 찾는다. 머리말 미국/한국 탭은 index.html·kr.html 양쪽에 있다.

(진행 중) 한국 탭 2단계 뉴스: `scripts/fetch_kr_news.py` → `docs/data/kr_news.json` → kr.html "주요 뉴스".
`fetch_news.gather`(네이버 API 우선)와 `fetch_hot_news.cluster_by_event(topic=...)`를 재사용. amazon.yml에서 kr_market 단계 뒤에 돈다.
한국 지수·업종은 장 마감 전에 돌면 오늘 봉을 빼고 종가만 쓴다(15:40 KST 이후 포함).

(진행 중) 한국 탭 3단계 공시: `scripts/fetch_dart.py` → `docs/data/kr_disclosures.json` → kr.html "공시".
OpenDART 공시검색(list.json)에서 유형 I(거래소공시)·B(주요사항)의 최근 3일을 받아 제목 낱말로 분류(`CATEGORIES`)해 30일 누적.
Secret `DART_API_KEY`. 잠정실적은 공시 원문(document.xml, zip)을 받아 Gemini가 매출·영업이익·순이익과 전년 동기 값을
옮기고(단위는 공시 그대로 받아 억원으로 통일), 증감률·흑자/적자전환은 코드가 계산(`fill_numbers`). 실행당 30건, 코스피 먼저.
kr.html "잠정실적 레이더" 화면이 이 데이터를 쓴다(주가 반응은 아직 없음).

(진행 중) 한국 탭 4단계 스크리너: `scripts/kr_breakout.py`(us_breakout의 판정·가격캐시 함수를 import해서 재사용, 일부만 교체)
→ `docs/data/kr_breakout.json` → kr.html "긴 조정 후 신고가"·"신고가·신저가". 워크플로우 `kr_screener.yml`(평일 18:50 KST, 가격캐시는 Actions 캐시).
종목 목록은 KRX KIND 상장법인 목록(유가증권=.KS, 코스닥=.KQ, 코넥스 제외). 시험에서 GitHub 서버에서도 KIND·야후 모두 잘 받아짐을 확인함
(`probe_kr.yml`/`probe_kr_sources.py`는 그 시험용). 시총 필터·실적 임박 배제·재무 게이트는 없음. 손으로 시험할 땐 Run workflow의 limit(종목 수)·force 입력을 쓴다.
야후는 틀린 접미사(.KQ↔.KS)에도 엉뚱한 데이터를 줄 수 있어 반드시 KIND 시장구분으로 접미사를 정한다.

(진행 중) 한국 탭 화면 보강(2026-09-30): "지수·업종"에 기간 버튼·마우스 오버 값 표시가 있는 큰 차트(코스피/코스닥, 업종 ETF 시작=100 비교)와
해설(kr_news.json의 event_notes 재사용), 새 화면 "섹터 온도"(`kr_sectors.json`, `kr_breakout.py`의 `sector_breadth`가 스크리너가 받은
전 종목 주가를 KIND 업종별로 묶어 중앙값 수익률·상승 비율·신고가/신저가 수를 계산). `fetch_kr_market.py`는 지수·업종 ETF를 5년치로
받아 1년은 매일·이전은 주별로 줄여 `history: [[날짜, 값]]`으로 저장한다. 화면 시험은 임시 웹서버(PowerShell HttpListener)로 사본을 띄워
직접 확인했다(저장소에는 넣지 않음).
차트가 미국보다 얇다는 지적에 따라 남은 보강 후보: 금리·환율 화면(국고채·환율 차트), 경제 지표(ECOS), 유동성·크레딧(ECOS), 주요 일정, 텔레그램 한국 요약(텍스트만).

1. 한국 탭: 머리말에 미국/한국 탭, 한국은 `kr.html` 별도. 순서 = 코스피·코스닥 지수·업종 + 국고채
   → 한국 뉴스 → DART 공시·잠정실적 → 한국 스크리너(us_breakout.py 로직, 종목 목록만 코스피·코스닥)
   → 수급. KRX는 해외 서버 차단 우려로 야후·DART·ECOS 등 공식 API 위주
2. (예전 목록) 관심종목 수급 — 외국인·기관 20일 누적

## 작업 방식

JH는 코딩 경험이 없다. 예전에는 브라우저에서 GitHub 웹 화면으로 파일을 올렸지만,
2026-09-30부터 Claude Code로 옮겨 로컬 저장소(`C:\Users\na\researchdashboard`)에서 직접 고친다.
git 설치와 clone 완료. 저장소는 Private이라 push에는 GitHub 로그인이 필요하다.

- 한 번에 하나씩 안내할 것. 여러 개를 묶어 설명하면 따라가기 어렵다.
- 파일은 직접 수정하고, commit/push 전에는 무엇을 바꿨는지 쉬운 말로 설명하고 JH에게 확인받는다.
- push하면 GitHub Pages·Actions에 바로 반영되므로 함부로 push하지 않는다.
- 스크립트는 `scripts` 폴더, 화면은 `docs` 폴더, 워크플로우는 `.github/workflows`.
- 실패하면 Actions 로그의 해당 단계를 캡처해서 보여준다
- 새 스크립트는 `continue-on-error: true`로 붙여 다른 단계를 막지 않게 한다
