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
python3 deploy/k3s/provision.py --apply    # 역할·DB·vector·Secret
kubectl apply -k deploy/k3s
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

## 자동 배포 (소스 수정 → 배포)

GitHub `hyungjin7/dify` main → `.github/workflows/sync-to-gitea.yml` → Gitea `hyungjin7/dify` main → `.gitea/workflows/ci.yml`.
CI 는 바뀐 폴더만 이미지로 빌드해 `gitea.delvrio.com/hyungjin7/dify/<api|web|agent|local-sandbox>:<sha>` 에 올리고 `kubectl apply -k` 로 배포한다.

| 바뀐 곳 | 이미지 | 바뀌는 배포 |
|---|---|---|
| `api/`, `dify-agent/` | api | dify-api·dify-api-ws·dify-worker·dify-beat |
| `web/`, `packages/`, `package.json`·`pnpm-lock.yaml` 등 | web | dify-web |
| `dify-agent/` | agent | dify-agent-backend |
| `dify-agent-runtime/` | local-sandbox | dify-local-sandbox |
| `deploy/k3s/` 만 | 빌드 없음 | 설정만 적용 |

- 우리 이미지가 아직 없는 부분은 공식 이미지(`langgenius/*:1.19.0`)로 돈다. 처음 한 번 전부 우리 이미지로 바꾸려면 Gitea › Actions › dify CI/CD › Run workflow, build=`all`.
- 처음 한 번 준비: GitHub 포크 Settings › Secrets 에 `GITEA_USER`·`GITEA_TOKEN`(odysseus 와 같은 값), Gitea 저장소에서 Actions 켜기. Secret `dify-db`·`dify-secret` 이 없으면 CI 는 빌드만 하고 배포는 건너뛴다.
- web 이미지는 빌드가 오래 걸린다(수십 분). api 를 고치면 DB 마이그레이션이 api 기동 때 자동으로 돈다.

## 업그레이드

이미지 태그 `1.19.0` 을 새 버전으로 바꾸고 `kubectl apply -k`. 새 버전의 `docker/envs/**/*.defaults`·`docker-compose.yaml` 에 새 서비스·환경 변수가 생겼는지 먼저 본다.

## 되돌리기

```bash
kubectl delete -k deploy/k3s          # 앱 전부(namespace 포함, PVC 도 지워진다)
# DB 까지 지우려면(복구 불가): prod-pg 에서 DROP DATABASE dify; DROP DATABASE dify_plugin; DROP ROLE dify;
redis-cli -h redis-service.bbb-namespace -n 10 FLUSHDB   # 11, 12 도 같게
```
