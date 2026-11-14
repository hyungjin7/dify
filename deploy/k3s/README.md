# Dify on k3s (dify.delvrio.com)

Dify 1.19.0 공식 이미지(`langgenius/*`)를 그대로 쓴다. 소스는 고치지 않고 이 폴더만 더했다(원본 Sync fork 가 충돌 없게).
원본 구성: `docker/docker-compose.yaml`. 여기서는 내장 Postgres·Redis·Weaviate 대신 **사내 CNPG `prod-pg` + 기존 Redis + pgvector** 를 쓴다.

## 구성

| 이름 | 하는 일 | 저장 |
|---|---|---|
| dify-nginx | 경로 나누기(Ingress → 여기) | - |
| dify-api / dify-api-ws | API·협업 웹소켓 (api 만 DB 마이그레이션) | PVC `dify-storage` |
| dify-worker / dify-beat | Celery 일꾼 / 예약(1개) | `dify-storage` |
| dify-web | 화면 | - |
| dify-plugin-daemon | 모델·도구 플러그인 | PVC `dify-plugin-storage`, DB `dify_plugin` |
| dify-sandbox + dify-ssrf-proxy | 코드 실행 칸막이 + 바깥 요청 검문소 | 휘발성 |
| dify-agent-backend, dify-local-sandbox, dify-agent-ssrf-proxy | Agent 셸 작업 공간 | 휘발성 |

- DB: `prod-pg-rw.postgre.svc.cluster.local:5432`, 역할 `dify`, DB `dify`(앱+벡터 pgvector)·`dify_plugin`. 연결 수 제한 80.
- Redis: `redis-service.bbb-namespace`(비밀번호 없음). bbb 가 0번을 쓰므로 Dify 는 10(캐시)·11(Celery)·12(Agent).
- 비밀: Secret `dify-db`(DB_HOST·DB_PORT·DB_USERNAME·DB_PASSWORD·DB_DATABASE·PGVECTOR_*), `dify-secret`(SECRET_KEY·INIT_PASSWORD·DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN 외 내부 키). `provision.py` 가 만들고 커밋하지 않는다.
- 로그인: Dify 자체 계정. `/install` 은 `INIT_PASSWORD` 를 알아야 열린다.

## 설치 (kubectl 되는 곳에서)

```bash
cd ~/projects/dify && git pull
python3 deploy/k3s/provision.py            # 미리보기(백업·pgvector·연결 수)
python3 deploy/k3s/provision.py --apply    # 역할·DB·vector·Secret + 배포(kubectl apply -k)
kubectl -n dify get pods -w                # dify-api 는 첫 기동 때 마이그레이션으로 몇 분
kubectl -n dify get secret dify-secret -o jsonpath='{.data.INIT_PASSWORD}' | base64 -d; echo
```

그다음 https://dify.delvrio.com/install → 위 비밀번호 → 관리자 계정을 만든다.

## LLM 연결 (oMLX 직접)

LLM Proxy 는 대기열 방식이라 Dify 의 스트리밍과 맞지 않아 **oMLX 에 직접 연결**한다(사용자 결정 2026-11-14). 사용기록은 Dify 자체 기록(앱 로그·토큰 수)에 남는다.

1. 설정 › 모델 공급자 › 마켓플레이스에서 **OpenAI-API-compatible** 설치
2. 모델 추가: Base URL `http://192.168.219.48:8000/v1`, 모델 `gemma-4-E4B-it-MLX-8bit`, API Key = oMLX 키(화면에만 입력)

검문소(`dify-ssrf-proxy`)는 사내망을 막으므로 oMLX 주소만 `SSRF_PROXY_ALLOW_PRIVATE_IPS=192.168.219.48/32` 로 열어 두었다. 다른 사내 주소가 필요하면 `app-sandbox.yaml` 에 더한다.
bbb 의 남는 칸 채우기(Idle Dispatcher)는 Dify 호출을 모른다. 둘이 겹쳐 느리면 bbb LLM 설정에서 reserve 를 1 로 둔다.

## 우리 RAG 연결 (2026-11-14)

bbb 의 RAG(뉴스·YouTube 색인, alpha-98 GPU)를 Dify 에서 두 가지로 쓴다. 둘 다 클러스터 안 주소라 바깥에 열리지 않는다.

1. **외부 지식(뉴스·YouTube 검색)** — 지식 › 외부 지식 API › 추가
   - Endpoint `http://rag-service.bbb-namespace.svc.cluster.local:8019/dify`
   - API Key: `kubectl -n bbb-namespace get secret rag-dify-key -o jsonpath='{.data.key}' | base64 -d; echo` (provision.py 가 만든다)
   - 외부 지식 ID: `all` · `news` · `youtube`, 주제로 좁히려면 `news:macro` 처럼(주제 macro·market·industry·company·policy·strategy·other)
   - 색인은 rag 가 계속 만든다. Dify 쪽에 다시 올리지 않는다. 검문소(`dify-ssrf-proxy`)는 rag 주소만 열어 두었다.
2. **임베딩·리랭커 모델(Dify 에 직접 올리는 문서용)** — 설정 › 모델 공급자 › **Text Embedding Inference** › 모델 추가(권장)
   - Text Embedding: 모델 `bge-m3`, Server url `http://embed-service.bbb-namespace.svc.cluster.local:8020/tei/bge-m3`
   - Rerank: 모델 `bge-reranker-v2-m3`, Server url `http://embed-service.bbb-namespace.svc.cluster.local:8020/tei/bge-reranker-v2-m3`
   - API Key 는 비워도 된다.

   또는 OpenAI-API-compatible › 모델 추가
   - Text Embedding: 모델 `bge-m3`, Base URL `http://embed-service.bbb-namespace.svc.cluster.local:8020/v1`, API Key 빈칸(아무 값), 컨텍스트 1024
   - Rerank: 모델 `bge-reranker-v2-m3`, 같은 Base URL
   - GPU 는 rag 색인과 한 줄로 번갈아 쓴다. 들어올 수 있는 곳은 bbb `app-embed.yaml` NetworkPolicy(dify-plugin-daemon 만).

## 소스 수정 → 배포 (사용자 결정 2026-11-14: 동기화만 자동, 배포는 손으로)

GitHub `hyungjin7/dify` main 은 `.github/workflows/sync-to-gitea.yml` 이 Gitea `hyungjin7/dify` main 으로 **동기화만** 한다. Gitea CI 는 자동으로 돌지 않는다.

- 설정(`deploy/k3s/`)만 바꿨으면: `git pull && python3 deploy/k3s/provision.py --apply`
- 소스(`api/`·`web/` 등)를 고쳤으면: Gitea › Actions › dify CI/CD › Run workflow(build = `api`, `web`, `agent`, `local-sandbox` 또는 `all`)로 이미지를 만들고 배포한다. 그 뒤 `provision.py --apply` 를 다시 돌려도 우리 이미지 태그는 유지된다.
- 처음 한 번: GitHub 포크 Secrets `GITEA_USER`·`GITEA_TOKEN`(odysseus 와 같은 값).

## 업그레이드

이미지 태그 `1.19.0` 을 새 버전으로 바꾸고 `kubectl apply -k`. 새 버전의 `docker/envs/**/*.defaults`·`docker-compose.yaml` 에 새 서비스·환경 변수가 생겼는지 먼저 본다.

## 되돌리기

```bash
kubectl delete -k deploy/k3s          # 앱 전부(namespace 포함, PVC 도 지워진다)
# DB 까지 지우려면(복구 불가): prod-pg 에서 DROP DATABASE dify; DROP DATABASE dify_plugin; DROP ROLE dify;
redis-cli -h redis-service.bbb-namespace -n 10 FLUSHDB   # 11, 12 도 같게
```
