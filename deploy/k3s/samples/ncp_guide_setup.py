#!/usr/bin/env python3
"""NCP 가이드 전용 지식 베이스 준비(한 번만). kubectl 되는 곳(맥북)에서:

  cd ~/projects/dify && git pull
  python3 deploy/k3s/samples/ncp_guide_setup.py

하는 일(다시 돌려도 안전):
  1) Dify 콘솔 로그인 → 지식 API 키 만들기(Secret dify/ncp-guide-sync 가 이미 있으면 그 키를 그대로 씀)
  2) 지식 베이스 "NCP 사용자 가이드" 찾기·없으면 만들기(고품질 색인 = 작업공간 기본 임베딩 모델, bge-m3)
  3) Secret dify/ncp-guide-sync {key, dataset_id} 저장 → CronJob ncp-guide-sync 가 이 값으로 갱신한다
그다음(출력에 명령이 나온다):
  - 첫 적재: kubectl -n dify create job --from=cronjob/ncp-guide-sync ncp-guide-first   (1~2시간, 밤 추천)
  - 질문 앱: SAVE_APP_KEY=ncp-guide-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/ncp-guide-chat.yml "VPC 만드는 방법"
비밀번호·키는 화면에 찍지 않는다. 표준 라이브러리만 쓴다.
"""
import base64
import getpass
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("DIFY_URL", "https://dify.delvrio.com").rstrip("/")
NAME = os.getenv("DATASET_NAME", "NCP 사용자 가이드")
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def call(method, url, body=None, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    else:
        req.add_header("X-CSRF-Token", next((c.value for c in jar if c.name.endswith("csrf_token")), ""))
    try:
        with opener.open(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:500]


def kubectl(*args, input_=None):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, input=input_)


def fail(msg, detail=None):
    print(f"\n[실패] {msg}")
    if detail is not None:
        print(json.dumps(detail, ensure_ascii=False)[:1200] if not isinstance(detail, str) else detail)
    sys.exit(1)


# 1) 지식 API 키
key = ""
r = kubectl("-n", "dify", "get", "secret", "ncp-guide-sync", "-o", "jsonpath={.data.key}")
if r.returncode == 0 and r.stdout:
    key = base64.b64decode(r.stdout).decode()
    print("[1] 기존 Secret dify/ncp-guide-sync 의 지식 API 키 사용")
else:
    email = os.getenv("DIFY_EMAIL") or input("Dify 이메일: ").strip()
    password = os.getenv("DIFY_PASSWORD") or getpass.getpass("Dify 비밀번호: ")
    st, res = call("POST", f"{BASE}/console/api/login",
                   {"email": email, "password": base64.b64encode(password.encode()).decode(), "remember_me": True})
    if st != 200 or not isinstance(res, dict) or res.get("result") != "success":
        fail(f"로그인 실패({st})", res)
    st, res = call("POST", f"{BASE}/console/api/datasets/api-keys", {})
    if st not in (200, 201) or not isinstance(res, dict) or not res.get("token"):
        fail(f"지식 API 키 만들기 실패({st})", res)
    key = res["token"]
    print("[1] 지식 API 키 새로 만듦")

# 2) 지식 베이스
st, res = call("GET", f"{BASE}/v1/datasets?page=1&limit=100&keyword=" + urllib.parse.quote(NAME), token=key)
if st != 200:
    fail(f"지식 목록 조회 실패({st})", res)
ds_id = next((d["id"] for d in res.get("data", []) if d.get("name") == NAME), "")
if ds_id:
    print(f"[2] 지식 베이스 있음: {NAME} ({ds_id})")
else:
    st, res = call("POST", f"{BASE}/v1/datasets", {"name": NAME, "indexing_technique": "high_quality", "permission": "only_me",
                                                    "description": "guide.ncloud-docs.com 사용자 가이드(llms.txt + .md, 주 1회 갱신). 다른 지식과 분리"},
                   token=key)
    if st not in (200, 201) or not isinstance(res, dict) or not res.get("id"):
        fail(f"지식 베이스 만들기 실패({st}). 설정 › 모델 공급자에서 기본 임베딩 모델(bge-m3)을 먼저 정하세요", res)
    ds_id = res["id"]
    print(f"[2] 지식 베이스 만듦: {NAME} ({ds_id})")

# 3) Secret
secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
          "metadata": {"name": "ncp-guide-sync", "namespace": "dify"},
          "stringData": {"key": key, "dataset_id": ds_id}}
r = kubectl("apply", "-f", "-", input_=json.dumps(secret))
if r.returncode != 0:
    fail("Secret 저장 실패", r.stderr)
print("[3] Secret dify/ncp-guide-sync 저장")
print(f"""
다음:
  1) 첫 적재(1~2시간, 밤 추천):
     kubectl -n dify create job --from=cronjob/ncp-guide-sync ncp-guide-first
     kubectl -n dify logs -f job/ncp-guide-first
  2) 질문 앱(ccc › NCP 가이드 묻기 가 이 앱을 부른다):
     SAVE_APP_KEY=ncp-guide-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/ncp-guide-chat.yml "VPC 만드는 방법"
  지식 베이스 화면: {BASE}/datasets/{ds_id}/documents""")
