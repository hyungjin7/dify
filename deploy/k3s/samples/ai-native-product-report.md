# 신규 창업아이템 AI 보고서 (AI Native 생산성 제품 창조) — 2026-11-24 확정

- 설계서(최종): https://claude.ai/artifact/B7ce6SBn3HGakCXWw3nfxH
- 흐름은 버핏 뉴스와 같은 3단계
  - ① 수집 = Dify 앱 **신규 창업아이템 ① 자료 수집**(`startup-collect-workflow.yml`, LLM 없음)
  - ② 분석 = bbb agent 가 저장 즉시 + 10분마다 자료 1건씩(Prompt Templates `startup.item.analyze`)
  - ③ 보고서 = Dify 앱 **신규 창업아이템 ③ 보고서**(`startup-report-workflow.yml`, Prompt Templates `Creating AI-Native Productivity Products`)
- 화면: i3 › AI › 신규 창업아이템 자료 조사 · 신규 창업아이템 보고서
- 시간표(검증 기간, KST): 수집 7·8·13·14시(8·14시는 검색 2쪽) · 보고서 9·15시 — Dashboard › 예약 작업 관리에서 바꿈
- 텔레그램: 버핏 뉴스와 다른 채널, Secret `startup-telegram`

## 1. 파일

| 곳 | 파일 | 하는 일 |
|---|---|---|
| dify | `startup-collect-workflow.yml` | Ego Lite 로 Google 8 + YouTube 4 검색 → 후보(웹 60·영상 24) → 중복 빼기 → 본문·자막 → agent 저장 |
| dify | `startup-report-workflow.yml` | agent 자료 50~80건 → Prompt Templates 보고서 → 출처 다시 쓰기·600자 점검 → agent 저장 → 텔레그램 |
| bbb | agent `startup_research.py` | 표 3개, Dify 창구 `/dify/startup/*`, 자료별 분석, 화면 API, 실행 주소 |
| bbb | agent `dify_llm.py` | `/dify/llm/template`(템플릿을 slug 또는 이름으로 찾음) |
| bbb | ego `main.py` | `/pages` 의 `include_links`, `/youtube/transcripts`(영상 여러 개) |
| bbb | dcore migration `operations 0038` | i3 대메뉴 AI + 화면 2개, 시간표 2개 |
| iii | `pages/ai/StartupResearchPage.tsx` · `StartupReportPage.tsx` | 화면 2개 |

## 2. 근거 규칙 (지어내기 막기)

- 출처·사이트·날짜·주소는 **서버가 저장한 값**으로 붙임. LLM 이 쓴 "참고 및 출처"는 버리고 본문에 인용한 `[자료N]` 만 다시 씀
- 웹 게시일은 ② 분석에서 본문에 적힌 날짜만 씀(본문에 없으면 "게시일 모름"). 영상은 YouTube 업로드일
- ② 의 근거 숫자는 본문에 글자 그대로 있는 것만 남김(없는 숫자는 버리고 건수만 기록)
- 자료에 없는 숫자·사례는 "(확인 필요)", 근거 약한 아이템은 "(가설)"

## 3. 준비·올리기 (kubectl 되는 맥북, bbb·iii PR 이 main 에 합쳐져 배포된 뒤)

```bash
# ① agent Dify 창구 키(한 번만)
kubectl -n bbb-namespace create secret generic agent-dify-key --from-literal=key="$(openssl rand -base64 32 | tr -d '/+=')"
kubectl -n bbb-namespace rollout restart deploy/agent

# ② 신규 창업 텔레그램 채널(채널에 봇을 관리자로 넣은 뒤, 한 번만)
cd ~/projects/dify && git pull
TELEGRAM_SECRET=startup-telegram python3 deploy/k3s/samples/telegram_setup.py

# ③ 수집 앱 올리기 → 지금 한 번 수집(10~20분)
SAVE_APP_KEY=startup-collect-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/startup-collect-workflow.yml

# ④ (자료 분석이 끝나면, 50건 ≈ 30~60분) 보고서 앱 올리기 → 지금 한 번 보고서 → 텔레그램
SAVE_APP_KEY=startup-report-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/startup-report-workflow.yml
```

- 템플릿 이름이 다르면 보고서 앱 환경 변수 `template_slug` 를 Prompt Templates 의 이름 또는 slug 로 고침
- 다음부터는 시간표가 돌고, 화면의 "지금 수집"·"지금 만들기"(관리자)로도 돌림
