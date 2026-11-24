#!/usr/bin/env python3
"""말씀 스와이프: 성경(개역한글)을 Dify 지식 "성경 개역한글"로 올린다 (2026-11-24 사용자 결정 "관련 구절=Dify 지식").
kubectl 되는 곳(맥북)에서, bible.delvrio.com 이 새 버전으로 떠 있을 때:

  cd ~/projects/dify && git pull
  python3 deploy/k3s/samples/bible_to_dify_knowledge.py

하는 일(다시 돌려도 안전, 바뀐 책만 다시 올림):
  1) 지식 API 키: Secret dify/bible-knowledge 가 있으면 그 키, 없으면 콘솔 로그인 후 새로 만듦
  2) 지식 베이스 "성경 개역한글" 찾기·없으면 만들기(고품질 색인, 임베딩 bge-m3). 다른 지식과 섞지 않는다
     찾는 방식: 하이브리드(뜻 + 낱말) + 리랭커(있으면), 조각 8개
  3) 책 66권을 앱 /api/export?book=N 에서 받아(원본 대조를 통과한 절만, 원본 문자열 그대로) 책마다 문서 1개로 올림
     한 절 = 한 조각("창세기 1:1 ‖ 본문"), 조각 나누기는 빈 줄, 공백 정리·URL 지우기 끔
     문서 이름 "01 창세기 · <지문 8자>" — 같은 지문이면 건너뛰고, 지문이 바뀐 책은 예전 문서를 지우고 다시 올림
  4) Secret dify/bible-knowledge {key, dataset_id} 저장
색인(임베딩)은 Dify 가 뒤에서 한다(3만 절, 수십 분). 비밀번호·키는 화면에 찍지 않는다. 표준 라이브러리만 쓴다.
"""
import base64
import getpass
import hashlib
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("DIFY_URL", "https://dify.delvrio.com").rstrip("/")
NAME = os.getenv("DATASET_NAME", "성경 개역한글")
APP = os.getenv("BIBLE_APP", "https://bible.delvrio.com").rstrip("/")
SECRET = "bible-knowledge"
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


_logged_in = False


def login():
    global _logged_in
    if _logged_in:
        return
    email = os.getenv("DIFY_EMAIL") or input("Dify 이메일: ").strip()
    password = os.getenv("DIFY_PASSWORD") or getpass.getpass("Dify 비밀번호: ")
    st, res = call("POST", f"{BASE}/console/api/login",
                   {"email": email, "password": base64.b64encode(password.encode()).decode(), "remember_me": True})
    if st != 200 or not isinstance(res, dict) or res.get("result") != "success":
        fail(f"로그인 실패({st})", res)
    _logged_in = True


def embedding_model():
    """작업공간 기본값에 기대지 않고 bge-m3(없으면 첫 임베딩 모델)를 직접 고른다."""
    login()
    st, res = call("GET", f"{BASE}/console/api/workspaces/current/models/model-types/text-embedding")
    if st != 200:
        fail(f"임베딩 모델 목록 실패({st})", res)
    cands = [(p["provider"], m["model"]) for p in (res or {}).get("data", []) for m in p.get("models", [])
             if m.get("status", "active") == "active"]
    want = os.getenv("EMBEDDING_MODEL", "bge-m3").lower()
    pick = next((c for c in cands if c[1].lower() == want), None) or next((c for c in cands if want in c[1].lower()), None) \
        or (cands[0] if cands else None)
    if not pick:
        fail("쓸 수 있는 임베딩 모델이 없습니다. 설정 › 모델 공급자에 bge-m3(Text Embedding Inference)를 먼저 추가하세요", res)
    print(f"    → 임베딩 모델 {pick[0]} / {pick[1]}")
    return pick


def rerank_model():
    """리랭커(bge-reranker-v2-m3, 없으면 첫 리랭커). 하나도 없으면 None → 뜻 검색만."""
    login()
    st, res = call("GET", f"{BASE}/console/api/workspaces/current/models/model-types/rerank")
    if st != 200:
        print(f"    (리랭커 목록 실패 {st}, 리랭커 없이 진행)")
        return None
    cands = [(p["provider"], m["model"]) for p in (res or {}).get("data", []) for m in p.get("models", [])
             if m.get("status", "active") == "active"]
    want = os.getenv("RERANK_MODEL", "bge-reranker-v2-m3").lower()
    pick = next((c for c in cands if c[1].lower() == want), None) or (cands[0] if cands else None)
    print(f"    → 리랭커 {pick[0]} / {pick[1]}" if pick else "    (리랭커 없음: 설정 › 모델 공급자에 bge-reranker-v2-m3 를 추가하면 더 잘 찾는다)")
    return pick



def app_get(path):
    req = urllib.request.Request(APP + path, headers={"User-Agent": "easybible-knowledge-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


# 1) 지식 API 키
key = ""
r = kubectl("-n", "dify", "get", "secret", SECRET, "-o", "jsonpath={.data.key}")
if r.returncode == 0 and r.stdout:
    key = base64.b64decode(r.stdout).decode()
    print(f"[1] 기존 Secret dify/{SECRET} 의 지식 API 키 사용")
else:
    login()
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
    provider, model = embedding_model()
    st, res = call("POST", f"{BASE}/v1/datasets", {"name": NAME, "indexing_technique": "high_quality", "permission": "only_me",
                                                    "embedding_model": model, "embedding_model_provider": provider,
                                                    "description": "말씀 스와이프: 개역한글 한 절 = 한 조각(원본 대조 통과한 절만). 관련 구절 찾기 전용"},
                   token=key)
    if st not in (200, 201) or not isinstance(res, dict) or not res.get("id"):
        fail(f"지식 베이스 만들기 실패({st})", res)
    ds_id = res["id"]
    print(f"[2] 지식 베이스 만듦: {NAME} ({ds_id})")
TOP_K = int(os.getenv("TOP_K", "8"))
rr = rerank_model()
if rr:
    retrieval = {"search_method": "hybrid_search", "reranking_enable": True, "reranking_mode": "reranking_model",
                 "reranking_model": {"reranking_provider_name": rr[0], "reranking_model_name": rr[1]},
                 "top_k": TOP_K, "score_threshold_enabled": False, "score_threshold": 0}
else:
    retrieval = {"search_method": "semantic_search", "reranking_enable": False, "top_k": TOP_K,
                 "score_threshold_enabled": False, "score_threshold": 0}
st, res = call("PATCH", f"{BASE}/v1/datasets/{ds_id}", {"retrieval_model": retrieval}, token=key)
if st != 200:
    fail(f"찾는 방식 설정 실패({st})", res)
print(f"    → 찾는 방식: {retrieval['search_method']}{' + 리랭커' if rr else ''}, 조각 {TOP_K}개")

# 3) 책 66권
docs, page = [], 1
while True:
    st, res = call("GET", f"{BASE}/v1/datasets/{ds_id}/documents?page={page}&limit=100", token=key)
    if st != 200:
        fail(f"문서 목록 실패({st})", res)
    docs += res.get("data", [])
    if not res.get("has_more"):
        break
    page += 1
have = {d["name"]: d["id"] for d in docs}
RULE = {"mode": "custom", "rules": {
    "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": False}, {"id": "remove_urls_emails", "enabled": False}],
    "segmentation": {"separator": "\n\n", "max_tokens": 1000}}}
added = skipped = verses = 0
for n in range(1, 67):
    try:
        d = app_get(f"/api/export?book={n}")
    except Exception as e:  # noqa: BLE001
        fail(f"앱에서 {n}번째 책을 못 받았어요: {e}. bible.delvrio.com 이 새 버전인지 확인하세요(rollout restart)")
    text = "\n\n".join(f"{v['ref']} ‖ {v['text']}" for v in d["verses"])
    prefix = f"{n:02d} {d['book']}"
    name = f"{prefix} · {hashlib.sha256(text.encode()).hexdigest()[:8]}"
    verses += len(d["verses"])
    if name in have:
        skipped += 1
        continue
    for old, oid in have.items():   # 지문이 바뀐 예전 문서는 지운다
        if old.startswith(prefix + " ·"):
            call("DELETE", f"{BASE}/v1/datasets/{ds_id}/documents/{oid}", token=key)
            print(f"    - 예전 문서 지움: {old}")
    st, res = call("POST", f"{BASE}/v1/datasets/{ds_id}/document/create-by-text",
                   {"name": name, "text": text, "indexing_technique": "high_quality", "doc_form": "text_model", "process_rule": RULE},
                   token=key)
    if st not in (200, 201):
        fail(f"{prefix} 올리기 실패({st})", res)
    added += 1
    print(f"    + {name} ({len(d['verses'])}절)")
print(f"[3] 올림 {added}권 · 그대로 {skipped}권 · 모두 {verses}절")

# 4) Secret
secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
          "metadata": {"name": SECRET, "namespace": "dify"},
          "stringData": {"key": key, "dataset_id": ds_id}}
r = kubectl("apply", "-f", "-", input_=json.dumps(secret))
if r.returncode != 0:
    fail("Secret 저장 실패", r.stderr)
print(f"[4] Secret dify/{SECRET} 저장")
print(f"""
색인(임베딩)은 뒤에서 진행돼요(수십 분). 진행: {BASE}/datasets/{ds_id}/documents
다음: 구절 공부 워크플로 다시 올리기
  SAVE_APP_KEY=easybible-dify-app SAVE_APP_KEY_NS=easybible INPUTS='{{"reference":"요한복음 3:16"}}' python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/bible-study-workflow.yml""")
