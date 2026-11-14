#!/usr/bin/env python3
"""뉴스·YouTube RAG 챗봇을 Dify 에 올리고(가져오기·키 넣기·게시) 실제로 한 번 질문해 결과를 보여 준다.

kubectl 되는 곳(맥북)에서:
  cd ~/projects/dify && git pull
  python3 deploy/k3s/samples/install_rag_chat.py                 # Dify 이메일·비밀번호를 묻는다
  python3 deploy/k3s/samples/install_rag_chat.py "반도체 업황 최근 뉴스 요약"   # 질문 바꾸기
  TOP_K=10 python3 deploy/k3s/samples/install_rag_chat.py               # 가져올 자료 수(3~20, 기본 6)

하는 일(다시 돌려도 같은 이름 앱을 덮어쓴다):
  1) RAG 키(Secret bbb-namespace/rag-dify-key)를 kubectl 로 읽어 앱 환경 변수(비밀값)에 넣는다
  2) LLM 은 OpenAI-API-compatible 에 등록된 모델 중 gemma(없으면 첫 모델)를 쓴다
  3) 가져오기 → 게시 → 앱 API 키(없으면 만든다) → /v1/chat-messages 로 질문, 답과 걸린 시간을 출력
비밀번호·키는 화면에 찍지 않는다. 표준 라이브러리만 쓴다.
"""
import base64
import getpass
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("DIFY_URL", "https://dify.delvrio.com").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
DSL = os.path.join(HERE, "rag-news-youtube-chat.yml")
APP_NAME = "뉴스·YouTube RAG 챗봇"
QUESTION = sys.argv[1] if len(sys.argv) > 1 else "최근 반도체 업황 관련 뉴스 요약해 줘"

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


# 1) RAG 키
step(1, "RAG 키 읽기(kubectl)")
try:
    key = subprocess.run(["kubectl", "-n", "bbb-namespace", "get", "secret", "rag-dify-key",
                          "-o", "jsonpath={.data.key}"], check=True, capture_output=True, text=True).stdout
    rag_key = base64.b64decode(key).decode()
except Exception as e:  # noqa: BLE001
    need(False, "rag-dify-key 를 못 읽었습니다. 먼저 python3 deploy/k3s/provision.py --apply", str(e))
need(bool(rag_key), "rag-dify-key 가 비었습니다")

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
dsl = dsl.replace("    value: ''\n    value_type: secret", f"    value: {json.dumps(rag_key)}\n    value_type: secret", 1)
dsl = dsl.replace("name: gemma-4-E4B-it-MLX-8bit", f"name: {json.dumps(pick[1])}", 1)
dsl = dsl.replace("provider: langgenius/openai_api_compatible/openai_api_compatible", f"provider: {json.dumps(pick[0])}", 1)
st, apps = call("GET", "/apps?page=1&limit=100&name=" + urllib.request.quote(APP_NAME))
existing = next((a["id"] for a in (apps or {}).get("data", []) if a.get("name") == APP_NAME), None) if st == 200 else None
body = {"mode": "yaml-content", "yaml_content": dsl}
if existing:
    body["app_id"] = existing
    print(f"    → 기존 앱 덮어쓰기 {existing}")
st, imp = call("POST", "/apps/imports", body)
if st == 202 and isinstance(imp, dict) and imp.get("status") == "pending":
    st, imp = call("POST", f"/apps/imports/{imp['id']}/confirm", {})
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

# 7) 실제 질문
step(7, f"질문: {QUESTION}")
t0 = time.monotonic()
st, ans = call("POST", f"{BASE}/v1/chat-messages",
               {"inputs": {"scope": "all", "top_k": os.getenv("TOP_K", "6")}, "query": QUESTION, "response_mode": "blocking", "user": "install-script"},
               token=tok, timeout=600)
sec = time.monotonic() - t0
need(st == 200 and isinstance(ans, dict) and ans.get("answer") is not None, f"질문 실패({st}, {sec:.1f}초)", ans)
print(f"\n===== 답 ({sec:.1f}초) =====\n{ans['answer']}\n")
usage = (ans.get("metadata") or {}).get("usage") or {}
if usage:
    print(f"토큰: 입력 {usage.get('prompt_tokens')} · 출력 {usage.get('completion_tokens')}")
print(f"앱 화면: {BASE}/app/{app_id}/workflow   대화 기록: {BASE}/app/{app_id}/logs")
