#!/usr/bin/env python3
"""Dify 앱 DSL(yml) 하나를 Dify 에 올리고(가져오기·키 넣기·게시) 실제로 한 번 질문해 결과를 보여 준다.

kubectl 되는 곳(맥북)에서:
  cd ~/projects/dify && git pull
  python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/naver-mainnews-summary.yml "오늘 주요뉴스 요약"
  APP_ID=<앱 id> python3 deploy/k3s/samples/install_app.py <앱.yml>      # 화면 주소 /app/<id>/workflow 의 앱을 덮어쓰기
  INPUTS='{"period": "30d"}' python3 deploy/k3s/samples/install_app.py <앱.yml> "질문"   # 시작 입력값
  SAVE_APP_KEY=news-dify-collect-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/news-collect-workflow.yml  # 버핏 뉴스 ① 수집(매시)
  SAVE_APP_KEY=news-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/news-hourly-summary-workflow.yml  # 버핏 뉴스 ③ 요약(8·12·15시), 앱 키를 bbb Secret 으로
  SAVE_APP_KEY=ncp-guide-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/ncp-guide-chat.yml "VPC 만드는 방법"  # ccc NCP 가이드
  SAVE_APP_KEY=tech-docs-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/tech-docs-chat.yml "Deployment 롤링 업데이트 설정"  # ccc 기술 문서
  SAVE_APP_KEY=docs-answer-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/docs-answer-workflow.yml "서브넷이 필요한 이유"  # ccc 골라 묻기
  SAVE_APP_KEY=startup-collect-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/startup-collect-workflow.yml  # 신규 창업아이템 ① 자료 수집(7·8·13·14시)
  SAVE_APP_KEY=startup-report-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/startup-report-workflow.yml  # 신규 창업아이템 ③ 보고서(9·15시)
  SAVE_APP_KEY=easybible-dify-app SAVE_APP_KEY_NS=easybible INPUTS='{"reference":"창세기 1장 1절"}' python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/bible-study-workflow.yml  # 말씀 스와이프 구절 공부(주소만, 비우면 랜덤, 키는 bible.delvrio.com 앱 namespace 로)
  python3 deploy/k3s/samples/bible_to_dify_knowledge.py   # 위 앱보다 먼저: 성경을 Dify 지식 "성경 개역한글"로(관련 구절 후보), 본문은 앱 bible.delvrio.com/api/verse

하는 일(다시 돌려도 같은 이름 앱을 덮어쓴다):
  1) DSL 이 rag_api_key·news_api_key·telegram_*·startup_telegram_* 을 쓰면 그 값(Secret bbb-namespace/rag-dify-key·news-dify-key·news-telegram·startup-telegram)을 kubectl 로 읽어 넣는다
  2) LLM 은 OpenAI-API-compatible 에 등록된 모델 중 gemma(없으면 첫 모델)를 쓴다
  3) 가져오기 → 게시 → 앱 API 키(없으면 만든다) → /v1/chat-messages 로 질문, 답과 걸린 시간을 출력
비밀번호·키는 화면에 찍지 않는다. 표준 라이브러리만 쓴다.
"""
import base64
import getpass
import http.cookiejar
import json
import re
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("DIFY_URL", "https://dify.delvrio.com").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
# 사용: python3 install_app.py <앱.yml> ["시험 질문"]   (APP_ID=<id> 를 주면 그 앱을 덮어쓴다)
DSL = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "naver-mainnews-summary.yml")
QUESTION = sys.argv[2] if len(sys.argv) > 2 else "오늘 주요뉴스 요약"
_name = [l for l in open(DSL, encoding="utf-8") if l.startswith("  name: ")]
APP_NAME = _name[0].split(":", 1)[1].strip().strip("'\"") if _name else ""

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def csrf():
    for c in jar:
        if c.name.endswith("csrf_token"):
            return c.value
    return ""


def call(method, path, body=None, token=None, timeout=60):
    url = path if path.startswith("http") else f"{BASE}/console/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        req.add_header("X-CSRF-Token", csrf())
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:500]


def need(ok, msg, detail=None):
    if not ok:
        print(f"\n[실패] {msg}")
        if detail is not None:
            print(json.dumps(detail, ensure_ascii=False, indent=1)[:1500] if not isinstance(detail, str) else detail)
        sys.exit(1)


def step(n, msg):
    print(f"[{n}] {msg}", flush=True)


# 1) DSL 이 쓰는 bbb 창구 키(환경 변수 이름 → Secret). 있는 것만 kubectl 로 읽는다.
SECRET_OF = {"rag_api_key": "rag-dify-key", "news_api_key": "news-dify-key",
             # agent Dify 창구(/dify/llm/template·/dify/startup/*: 신규 창업아이템, 준비: samples/ai-native-product-report.md)
             "agent_api_key": "agent-dify-key",
             # 텔레그램(준비: samples/telegram_setup.py) — Secret 이름:칸 이름
             "telegram_bot_token": "news-telegram:token", "telegram_chat_id": "news-telegram:chat_id",
             # 신규 창업 보고서 전용 채널(준비: TELEGRAM_SECRET=startup-telegram samples/telegram_setup.py)
             "startup_telegram_bot_token": "startup-telegram:token",
             "startup_telegram_chat_id": "startup-telegram:chat_id"}
step(1, "bbb 창구 키 읽기(kubectl, DSL 이 쓰는 것만)")
env_keys = {}
_dsl_text = open(DSL, encoding="utf-8").read()
for var, sec in SECRET_OF.items():
    if f"name: {var}" not in _dsl_text:
        continue
    try:
        name, _, field = sec.partition(":")
        key = subprocess.run(["kubectl", "-n", "bbb-namespace", "get", "secret", name,
                              "-o", "jsonpath={.data." + (field or "key") + "}"], check=True, capture_output=True, text=True).stdout
        need(bool(key), f"Secret {name} 에 {field or 'key'} 칸이 없습니다")
        env_keys[var] = base64.b64decode(key).decode()
        print(f"    → {var} ← Secret {sec}")
    except Exception as e:  # noqa: BLE001
        hint = ("TELEGRAM_SECRET=startup-telegram python3 deploy/k3s/samples/telegram_setup.py" if var.startswith("startup_telegram")
                else "python3 deploy/k3s/samples/telegram_setup.py" if var.startswith("telegram")
                else "deploy/k3s/samples/ai-native-product-report.md 의 '준비' 명령" if var == "agent_api_key"
                else "python3 deploy/k3s/provision.py --apply")
        need(False, f"{sec} 를 못 읽었습니다. 먼저 {hint}", str(e))
if not env_keys:
    print("    → 필요 없음")

# 2) 로그인
email = os.getenv("DIFY_EMAIL") or input("Dify 이메일: ").strip()
password = os.getenv("DIFY_PASSWORD") or getpass.getpass("Dify 비밀번호: ")
step(2, "Dify 로그인")
st, res = call("POST", "/login", {"email": email, "password": base64.b64encode(password.encode()).decode(),
                                  "remember_me": True})
need(st == 200 and isinstance(res, dict) and res.get("result") == "success", f"로그인 실패({st})", res)

# 3) LLM 모델 고르기
step(3, "LLM 모델 찾기")
st, res = call("GET", "/workspaces/current/models/model-types/llm")
need(st == 200, f"모델 목록 실패({st})", res)
cands = [(p["provider"], m["model"]) for p in res.get("data", []) for m in p.get("models", [])
         if m.get("status", "active") == "active"]
need(bool(cands), "쓸 수 있는 LLM 이 없습니다. 설정 › 모델 공급자에서 oMLX 모델을 먼저 추가하세요", res)
pick = next((c for c in cands if "gemma" in c[1].lower()), None) or \
    next((c for c in cands if "openai_api_compatible" in c[0]), cands[0])
print(f"    → {pick[0]} / {pick[1]}")

# 4) DSL 채우기(키·모델) 후 가져오기(같은 이름 앱이 있으면 덮어쓰기)
step(4, "앱 가져오기")
dsl = open(DSL, encoding="utf-8").read()
for var, val in env_keys.items():   # 환경 변수(비밀) 값 채우기: "name: <var>" 다음의 빈 value
    dsl = dsl.replace(f"    name: {var}\n    value: ''\n", f"    name: {var}\n    value: {json.dumps(val)}\n", 1)
if "__NCP_DATASET_ID__" in dsl:   # NCP 가이드 앱: 전용 지식 베이스 id(ncp_guide_setup.py 가 Secret 에 저장)
    ds = os.getenv("DATASET_ID", "").strip()
    if not ds:
        r_ = subprocess.run(["kubectl", "-n", "dify", "get", "secret", "ncp-guide-sync", "-o", "jsonpath={.data.dataset_id}"],
                            capture_output=True, text=True)
        ds = base64.b64decode(r_.stdout).decode() if r_.returncode == 0 and r_.stdout else ""
    need(bool(ds), "NCP 지식 베이스 id 가 없습니다. 먼저 python3 deploy/k3s/samples/ncp_guide_setup.py")
    dsl = dsl.replace("__NCP_DATASET_ID__", ds)
    print(f"    → 지식 베이스 {ds}")
for ds_name in sorted(set(re.findall(r"__DS:([^_\n<>]+)__", dsl))):   # 기술 문서 앱: 지식 베이스 이름 → id
    st, res = call("GET", "/datasets?page=1&limit=100&keyword=" + urllib.request.quote(ds_name))
    ds = next((d["id"] for d in (res or {}).get("data", []) if d.get("name") == ds_name), "") if st == 200 else ""
    need(bool(ds), f"지식 베이스 '{ds_name}' 가 없습니다. 첫 적재가 끝나야 합니다: "
                   "kubectl -n dify logs -f deploy/knowledge-collector 로 진행을 보세요")
    dsl = dsl.replace(f"__DS:{ds_name}__", ds)
    print(f"    → {ds_name} = {ds}")
dsl = dsl.replace("name: gemma-4-E4B-it-MLX-8bit", f"name: {json.dumps(pick[1])}", 1)
dsl = dsl.replace("provider: langgenius/openai_api_compatible/openai_api_compatible", f"provider: {json.dumps(pick[0])}", 1)
# 덮어쓸 앱 찾기(새 앱을 만들지 않게, 2026-11-19 사용자 지시 "만들지 말고 업데이트"):
#   ① APP_ID  ② SAVE_APP_KEY Secret 에 저장된 키를 가진 앱(이름을 바꿔도 찾는다)  ③ 같은 이름 앱
st, apps = call("GET", "/apps?page=1&limit=100")
all_apps = (apps or {}).get("data", []) if st == 200 else []
existing, how = os.getenv("APP_ID"), "APP_ID"
_save = os.getenv("SAVE_APP_KEY", "").strip()
if not existing and _save:
    r_ = subprocess.run(["kubectl", "-n", "bbb-namespace", "get", "secret", _save, "-o", "jsonpath={.data.key}"],
                        capture_output=True, text=True)
    saved_tok = base64.b64decode(r_.stdout).decode() if r_.returncode == 0 and r_.stdout else ""
    for a in all_apps if saved_tok else []:
        st_k, ks = call("GET", f"/apps/{a['id']}/api-keys")
        if st_k == 200 and any(k.get("token") == saved_tok for k in (ks or {}).get("data", [])):
            existing, how = a["id"], f"Secret {_save} 의 키를 가진 앱 '{a.get('name')}'"
            break
# 이름을 바꾼 앱은 예전 이름으로도 찾는다(새 이름: 예전 이름들)
OLD_NAMES = {"버핏 뉴스 ③ 요약": ["버핏 뉴스 요약", "뉴스 시간별 요약"], "버핏 뉴스 요약": ["뉴스 시간별 요약"]}
same = [a for a in all_apps if a.get("name") == APP_NAME]
if not existing and not same:
    same = [a for a in all_apps if a.get("name") in OLD_NAMES.get(APP_NAME, [])]
if not existing and same:
    existing, how = same[0]["id"], f"이름 '{same[0].get('name')}'"
# 같은 이름(또는 예전 이름) 중복 앱: 기본은 목록만 보여 준다(되돌릴 수 없으니 목록을 보고 고른다).
# 지우기: DELETE_DUPS=1 (덮어쓸 앱 하나만 남기고 나머지 삭제)
names = {APP_NAME, *OLD_NAMES.get(APP_NAME, [])}
dups = [a for a in all_apps if a.get("name") in names and a["id"] != existing] if existing else []
for a in dups:
    if os.getenv("DELETE_DUPS") != "1":
        print(f"    → 중복 앱(지우려면 DELETE_DUPS=1): '{a.get('name')}' {a['id']} 수정 {a.get('updated_at') or '-'}  {BASE}/app/{a['id']}/workflow")
        continue
    st_d, res_d = call("DELETE", f"/apps/{a['id']}")
    print(f"    → 중복 앱 삭제 '{a.get('name')}' {a['id']}: " + ("완료" if st_d in (200, 204) else f"실패({st_d}) {res_d}"))
if existing:
    print(f"    → 덮어쓸 앱: {how}")
body = {"mode": "yaml-content", "yaml_content": dsl}
if existing:
    body["app_id"] = existing
    print(f"    → 기존 앱 덮어쓰기 {existing}")
st, imp = call("POST", "/apps/imports", body)
if st == 202 and isinstance(imp, dict) and imp.get("status") == "pending":
    st, imp = call("POST", f"/apps/imports/{imp['id']}/confirm", {})
if isinstance(imp, dict) and "incompatible" in str(imp.get("error") or ""):
    print("    → 덮어쓸 앱과 DSL 의 앱 종류(챗봇/워크플로)가 다릅니다. 워크플로 앱이면 *-workflow.yml 을, 아니면 APP_ID 없이 새 앱으로 올리세요")
need(st == 200 and isinstance(imp, dict) and imp.get("app_id"), f"가져오기 실패({st})", imp)
app_id = imp["app_id"]
print(f"    → 앱 {app_id}  {BASE}/app/{app_id}/workflow")

# 5) 게시
step(5, "게시")
st, res = call("POST", f"/apps/{app_id}/workflows/publish", {"marked_name": "rag-chat"})
need(st in (200, 201), f"게시 실패({st})", res)

# 6) 앱 API 키
step(6, "앱 API 키")
st, keys = call("GET", f"/apps/{app_id}/api-keys")
tok = (keys or {}).get("data", [{}])[0].get("token") if st == 200 and (keys or {}).get("data") else None
if not tok:
    st, k = call("POST", f"/apps/{app_id}/api-keys", {})
    need(st in (200, 201) and isinstance(k, dict) and k.get("token"), f"API 키 만들기 실패({st})", k)
    tok = k["token"]

# 6-1) 앱 키를 bbb 가 쓰게 Secret 으로 저장(SAVE_APP_KEY=<Secret 이름>, 예: news-dify-app → news 가 이 앱을 부른다)
save_as = os.getenv("SAVE_APP_KEY", "").strip()
save_ns = os.getenv("SAVE_APP_KEY_NS", "").strip() or "bbb-namespace"   # 다른 앱 namespace 에 둘 때(예: easybible)
if save_as:
    subprocess.run(["kubectl", "apply", "-f", "-"], check=True, capture_output=True, input=json.dumps(
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": save_as, "namespace": save_ns}, "stringData": {"key": tok}}).encode())
    user_of = {"news-dify-app": "news", "news-dify-collect-app": "news", "ncp-guide-dify-app": "agent", "tech-docs-dify-app": "agent", "docs-answer-dify-app": "agent",
               "startup-collect-dify-app": "agent", "startup-report-dify-app": "agent"}   # 이 키를 쓰는 bbb 서비스
    svc = user_of.get(save_as)
    if svc:
        subprocess.run(["kubectl", "-n", "bbb-namespace", "rollout", "restart", f"deploy/{svc}"], check=True, capture_output=True)
    if save_as == "easybible-dify-app":   # 말씀 스와이프(namespace easybible)가 새 키를 읽게. 아직 배포 전이면 건너뜀
        subprocess.run(["kubectl", "-n", save_ns, "rollout", "restart", "deploy/easybible"], capture_output=True)
    print(f"    → 앱 키를 Secret {save_ns}/{save_as} 에 저장" + (f"하고 {svc} 를 다시 띄웠습니다" if svc else ""))

# 6-2) 앱 키를 이 컴퓨터의 .env 파일에 넣기(ENV_FILE=<경로>, 예: 말씀 스와이프 앱 easybible/.env.local).
#      DIFY_API_URL·DIFY_API_KEY 두 줄만 바꾸고 나머지 줄은 그대로 둔다. 키는 화면에 찍지 않는다.
env_file = os.path.expanduser(os.getenv("ENV_FILE", "").strip())
if env_file:
    want = {"DIFY_API_URL": f"{BASE}/v1", "DIFY_API_KEY": tok}
    lines = open(env_file, encoding="utf-8").read().splitlines() if os.path.exists(env_file) else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in want]
    lines += [f"{k}={v}" for k, v in want.items()]
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(env_file, 0o600)
    print(f"    → 앱 키를 {env_file} 에 저장(DIFY_API_URL·DIFY_API_KEY)")

# 7) 시험 실행 — 이름표는 실제로 하는 일로 보인다(질문을 보내는 앱만 "질문: …")
RUN_LABELS = {"news-dify-app": "지금 시각 요약 한 번(news 를 통해)", "news-dify-collect-app": "지금 한 번 뉴스 수집(news 를 통해)",
              "startup-collect-dify-app": "지금 한 번 자료 수집(agent 를 통해, 10~20분)",
              "startup-report-dify-app": "지금 한 번 보고서(agent 를 통해, 5~15분)"}
if save_as in RUN_LABELS:
    label = RUN_LABELS[save_as]
elif "\n  mode: workflow" in dsl and not ("variable: question" in dsl or "variable: request" in dsl):
    label = "시험 실행(질문 없는 워크플로)"
else:
    label = f"질문: {QUESTION}"
step(7, label)
t0 = time.monotonic()
inputs = json.loads(os.getenv("INPUTS", "{}"))
if save_as in ("news-dify-app", "news-dify-collect-app"):
    # 시간별 요약: Dify 를 바로 부르면 news 에 저장이 안 돼 i3 이력에 안 나온다 → news 를 통해 지금 시간을 다시 돌린다
    subprocess.run(["kubectl", "-n", "bbb-namespace", "rollout", "status", "deploy/news", "--timeout=300s"],
                   check=True, capture_output=True)
    # ③ 요약은 지금 시각을 다시 요약, ① 수집은 지금 한 번 수집(2026-11-22 2단계 분리)
    _run_path = "hourly-summary/run?force=true" if save_as == "news-dify-app" else "hourly-collect/run"
    # news 는 서비스 신원이 있어야 받는다(없으면 401). 매시 정각에 부르는 dworker 의 신원(SERVICE_AUTH_*)으로 부른다.
    code_ = ("import os,urllib.request;r=urllib.request.urlopen(urllib.request.Request("
             f"'http://news-service.bbb-namespace.svc.cluster.local:8005/api/v1/news/{_run_path}',"
             "method='POST',headers={'X-Service-Name':os.environ['SERVICE_AUTH_NAME'],"
             "'X-Service-Key':os.environ['SERVICE_AUTH_KEY']}),timeout=900);print(r.read().decode())")
    # 다시 띄운 직후에는 끝나 가는 옛 Pod 로 연결이 가서 'Connection refused' 가 날 수 있다(2026-11-19 실측, 21.7초 실패).
    # 그때만 10초씩 쉬며 6번까지 다시 부른다. 다른 실패는 그대로 보여 준다.
    for attempt in range(6):
        out = subprocess.run(["kubectl", "-n", "bbb-namespace", "exec", "deploy/dworker", "--", "python", "-c", code_],
                             capture_output=True, text=True)
        if out.returncode == 0 or "Connection refused" not in out.stderr:
            break
        print(f"    → news 가 아직 연결을 안 받음(Connection refused), 10초 뒤 다시 ({attempt + 1}/6)")
        time.sleep(10)
    sec = time.monotonic() - t0
    res = {}
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    need(res.get("status") == "done", f"news 를 통한 실행 실패({sec:.1f}초)", out.stdout[-1500:] + out.stderr[-1500:])
    print(f"\n===== news 저장 완료 ({sec:.1f}초) · 기사 {res.get('articles')}건 · {res.get('slot')} =====")
    print("i3 News › 뉴스 요약(시간별) 이력과 텔레그램에서 확인하세요")
elif save_as in ("startup-collect-dify-app", "startup-report-dify-app"):
    # 신규 창업아이템: Dify 를 바로 부르면 수집 기록·보고서 번호가 안 남는다 → agent 를 통해 지금 한 번 돌린다(시간표와 같은 길)
    subprocess.run(["kubectl", "-n", "bbb-namespace", "rollout", "status", "deploy/agent", "--timeout=300s"],
                   check=True, capture_output=True)
    _run_path = "collect/run" if save_as == "startup-collect-dify-app" else "report/run"
    code_ = ("import os,urllib.request;r=urllib.request.urlopen(urllib.request.Request("
             f"'http://agent-service.bbb-namespace.svc.cluster.local:8000/api/v1/agents/startup/{_run_path}',"
             "data=b'{}',method='POST',headers={'Content-Type':'application/json','X-Service-Name':os.environ['SERVICE_AUTH_NAME'],"
             "'X-Service-Key':os.environ['SERVICE_AUTH_KEY']}),timeout=1600);print(r.read().decode())")
    for attempt in range(6):
        out = subprocess.run(["kubectl", "-n", "bbb-namespace", "exec", "deploy/dworker", "--", "python", "-c", code_],
                             capture_output=True, text=True)
        if out.returncode == 0 or "Connection refused" not in out.stderr:
            break
        print(f"    → agent 가 아직 연결을 안 받음(Connection refused), 10초 뒤 다시 ({attempt + 1}/6)")
        time.sleep(10)
    sec = time.monotonic() - t0
    res = {}
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    need(res.get("status") == "done", f"agent 를 통한 실행 실패({sec:.1f}초)", out.stdout[-1500:] + out.stderr[-1500:])
    if save_as == "startup-collect-dify-app":
        print(f"\n===== 수집 완료 ({sec:.1f}초) · 저장 {res.get('saved')}건 · 부족 {res.get('shortfall')}건 =====")
        print("자료별 분석(②)은 agent 가 지금부터 한 건씩 함(50건 ≈ 30~60분). i3 › AI › 신규 창업아이템 자료 조사에서 '분석 끝' 수 확인")
    else:
        print(f"\n===== 보고서 완료 ({sec:.1f}초) · 보고서 #{res.get('report_id')} · 텔레그램 {res.get('telegram')} =====")
        print("i3 › AI › 신규 창업아이템 보고서에서 확인")
elif "\n  mode: workflow" in dsl:
    # 워크플로 앱: 질문은 시작 입력 request 로 넣고 /v1/workflows/run 의 outputs 를 본다
    if "variable: question" in dsl:   # 문서 골라 묻기: 질문 칸 이름이 question
        inputs.setdefault("question", QUESTION)
    else:
        inputs.setdefault("request", QUESTION)
    if "variable: slot" in dsl:     # 시간별 요약: 지금 KST 정각
        from datetime import datetime, timedelta, timezone
        inputs.setdefault("slot", (datetime.now(timezone(timedelta(hours=9)))).strftime("%Y-%m-%d %H:00"))
    st, ans = call("POST", f"{BASE}/v1/workflows/run",
                   {"inputs": inputs, "response_mode": "blocking", "user": "install-script"}, token=tok, timeout=600)
    sec = time.monotonic() - t0
    data = (ans or {}).get("data") or {} if isinstance(ans, dict) else {}
    need(st == 200 and data.get("status") == "succeeded", f"실행 실패({st}, {sec:.1f}초)", ans)
    outs = data.get("outputs") or {}
    print(f"\n===== 결과 ({sec:.1f}초) =====\n" + "\n\n".join(str(v) for v in outs.values()) + "\n")
    print(f"토큰: {data.get('total_tokens')}")
else:
    st, ans = call("POST", f"{BASE}/v1/chat-messages",
                   {"inputs": inputs, "query": QUESTION, "response_mode": "blocking", "user": "install-script"},
                   token=tok, timeout=600)
    sec = time.monotonic() - t0
    need(st == 200 and isinstance(ans, dict) and ans.get("answer") is not None, f"질문 실패({st}, {sec:.1f}초)", ans)
    print(f"\n===== 답 ({sec:.1f}초) =====\n{ans['answer']}\n")
    usage = (ans.get("metadata") or {}).get("usage") or {}
    if usage:
        print(f"토큰: 입력 {usage.get('prompt_tokens')} · 출력 {usage.get('completion_tokens')}")
print(f"앱 화면: {BASE}/app/{app_id}/workflow   대화 기록: {BASE}/app/{app_id}/logs")
