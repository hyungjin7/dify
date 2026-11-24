# AI Native 생산성 제품 창조 — Dify 워크플로 설계 (2026-11-24)

- 사용자 요청: Dify 에 제목 "AI Native 생산성 제품 창조" 로, 사용자 시스템 프롬프트를 써서 CEO 용 One-Page 보고서를 만든다. 기본 흐름은 버핏 뉴스와 같게.
- 파일: `ai-native-product-report-workflow.yml`(Dify DSL, 워크플로 앱). 아직 Dify 에 올리지 않음(사용자 확인 뒤).

## 1. 흐름 (버핏 뉴스 ③ 요약과 같은 뼈대)

| 순서 | 버핏 뉴스 ③ 요약 | AI Native 생산성 제품 창조 |
|---|---|---|
| 1 | 시작(slot) | 시작(관심 분야·기간·자료 수·작성일) |
| 2 | 요약할 기사 모으기(news) | 검색 요청 만들기(code) → 우리 RAG 3갈래 동시 검색 |
| 3 | 버핏 판정 결과 모으기 → 정리 | 근거 자료 정리(중복 제거, `[자료N]`·게시일) |
| 4 | gemma 한눈에(LLM) | gemma One-Page 보고서(LLM, 사용자 프롬프트 그대로 + 근거 규칙) |
| 5 | 결과 정리(출처 다시 씀) | 결과 정리(인용한 자료만 출처로, 분량 점검) |
| 6 | 텔레그램 → 끝 | 텔레그램 → 끝 |

- RAG 3갈래(`rag-service:8019/dify/retrieval`, `knowledge_id=all:<기간>`): ① AI 창업·투자 ② 생성형 AI 업무 자동화 ③ AI 코딩·개발 도구. 관심 분야를 넣으면 검색어 앞에 붙는다.
- 키: `rag_api_key`(Secret `rag-dify-key`), `telegram_*`(Secret `news-telegram`, 버핏 뉴스와 같은 방). `install_app.py` 가 자동으로 넣는다.

## 2. 버핏 뉴스에서 뺀 단계와 이유

- ego 수집·news 중복 확인·저장: 새 기사를 모으는 일이 아니라 이미 모은 뉴스·YouTube·리포트에서 근거를 찾는 일임.
- 시장 소음 제목 규칙·시총 1조 범위: 주가 소음과 상장 종목을 거르는 규칙이라 창업 아이템과 무관함.
- 해자 5항목 점수·DART 대조: 상장사 재무·공시 검증용임. 창업 아이템은 공시가 없음. 대신 프롬프트의 Moat 열로 대신함.
- 상시 판정(② 단계): 기사 수백 건을 나눠 판정하는 구조임. 이 보고서는 1편이라 필요 없음.
- news_hourly_summaries 저장: news 전용 표임. 저장처는 아래 결정 뒤 정함(지금은 Dify 실행 기록에 남음).

## 3. 근거 규칙 (프롬프트 뒤에 덧붙임)

- 통계·금액·사례는 `<자료>` 에 있는 것만, `[자료N, 게시일]` 로 붙임. 없으면 "(확인 필요)".
- 추천 아이템이 근거가 약하면 "(가설)".
- "참고 및 출처" 는 LLM 이 쓴 것을 버리고 실제 인용한 `[자료N]` 만 종류·채널·게시일·링크로 다시 씀. 인용이 없으면 "확인 필요".
- 분량: 표·머리글을 뺀 본문 글자 수(공백 제외)를 재서 1000자 넘으면 보고서 끝에 ⚠️ 표시.

## 4. 올리기 (사용자 확인 뒤)

```bash
cd ~/projects/dify && git pull
python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/ai-native-product-report-workflow.yml
INPUTS='{"focus": "물류 정산", "period": "30d"}' python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/ai-native-product-report-workflow.yml
```

## 5. 남은 결정

- 실행 시점: 손으로(Dify 화면) / 정기(예: 매주 월 08:00 — bbb dworker 시간표·저장 API 필요).
- 근거 범위: 우리 RAG 만(투자 뉴스 위주라 창업·스타트업 자료가 얕을 수 있음) / 웹 검색 도구 추가.
- 텔레그램: 버핏 뉴스 방에 같이 보냄 / 끔.
