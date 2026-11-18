#!/usr/bin/env python3
"""Dify 앱 DSL(yml) 하나를 Dify 에 올리고(가져오기·키 넣기·게시) 실제로 한 번 질문해 결과를 보여 준다.

kubectl 되는 곳(맥북)에서:
  cd ~/projects/dify && git pull
  python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/naver-mainnews-summary.yml "오늘 주요뉴스 요약"
  APP_ID=<앱 id> python3 deploy/k3s/samples/install_app.py <앱.yml>      # 화면 주소 /app/<id>/workflow 의 앱을 덮어쓰기
  INPUTS='{"period": "30d"}' python3 deploy/k3s/samples/install_app.py <앱.yml> "질문"   # 시작 입력값
  SAVE_APP_KEY=news-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/news-hourly-summary-workflow.yml  # 앱 키를 bbb Secret 으로
  SAVE_APP_KEY=ncp-guide-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/ncp-guide-chat.yml "VPC 만드는 방법"  # ccc NCP 가이드
  SAVE_APP_KEY=tech-docs-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/tech-docs-chat.yml "Deployment 롤링 업데이트 설정"  # ccc 기술 문서
  SAVE_APP_KEY=docs-answer-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/docs-answer-workflow.yml "서브넷이 필요한 이유"  # ccc 골라 묻기

하는 일(다시 돌려도 같은 이름 앱을 덮어쓴다):
  1) DSL 이 rag_api_key·news_api_key·telegram_* 을 쓰면 그 값(Secret bbb-namespace/rag-dify-key·news-dify-key·news-telegram)을 kubectl 로 읽어 넣는다
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
             # 텔레그램(준비: samples/telegram_setup.py) — Secret 이름:칸 이름
             "telegram_bot_token": "news-telegram:token", "telegram_chat_id": "news-telegram:chat_id"}
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
        hint = "python3 deploy/k3s/samples/telegram_setup.py" if var.startswith("telegram") else "python3 deploy/k3s/provision.py --apply"
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
st, apps = call("GET", "/apps?page=1&limit=100&name=" + urllib.request.quote(APP_NAME))
existing = next((a["id"] for a in (apps or {}).get("data", []) if a.get("name") == APP_NAME), None) if st == 200 else None
existing = os.getenv("APP_ID") or existing   # 화면 주소 /app/<id>/workflow 의 id 를 주면 그 앱을 덮어쓴다
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
if save_as:
    subprocess.run(["kubectl", "apply", "-f", "-"], check=True, capture_output=True, input=json.dumps(
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": save_as, "namespace": "bbb-namespace"}, "stringData": {"key": tok}}).encode())
    user_of = {"news-dify-app": "news", "ncp-guide-dify-app": "agent", "tech-docs-dify-app": "agent", "docs-answer-dify-app": "agent"}   # 이 키를 쓰는 bbb 서비스
    svc = user_of.get(save_as)
    if svc:
        subprocess.run(["kubectl", "-n", "bbb-namespace", "rollout", "restart", f"deploy/{svc}"], check=True, capture_output=True)
    print(f"    → 앱 키를 Secret bbb-namespace/{save_as} 에 저장" + (f"하고 {svc} 를 다시 띄웠습니다" if svc else ""))

# 7) 실제 질문
step(7, f"질문: {QUESTION}")
t0 = time.monotonic()
inputs = json.loads(os.getenv("INPUTS", "{}"))
if save_as == "news-dify-app":
    # 시간별 요약: Dify 를 바로 부르면 news 에 저장이 안 돼 i3 이력에 안 나온다 → news 를 통해 지금 시간을 다시 돌린다
    subprocess.run(["kubectl", "-n", "bbb-namespace", "rollout", "status", "deploy/news", "--timeout=300s"],
                   check=True, capture_output=True)
    # news 는 서비스 신원이 있어야 받는다(없으면 401). 매시 정각에 부르는 dworker 의 신원(SERVICE_AUTH_*)으로 부른다.
    code_ = ("import os,urllib.request;r=urllib.request.urlopen(urllib.request.Request("
             "'http://news-service.bbb-namespace.svc.cluster.local:8005/api/v1/news/hourly-summary/run?force=true',"
             "method='POST',headers={'X-Service-Name':os.environ['SERVICE_AUTH_NAME'],"
             "'X-Service-Key':os.environ['SERVICE_AUTH_KEY']}),timeout=900);print(r.read().decode())")
    out = subprocess.run(["kubectl", "-n", "bbb-namespace", "exec", "deploy/dworker", "--", "python", "-c", code_],
                         capture_output=True, text=True)
    sec = time.monotonic() - t0
    res = {}
    try:
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        pass
    need(res.get("status") == "done", f"news 를 통한 실행 실패({sec:.1f}초)", out.stdout[-1500:] + out.stderr[-1500:])
    print(f"\n===== news 저장 완료 ({sec:.1f}초) · 기사 {res.get('articles')}건 · {res.get('slot')} =====")
    print("i3 News › 뉴스 요약(시간별) 이력과 텔레그램에서 확인하세요")
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
