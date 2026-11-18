#!/usr/bin/env python3
"""NAVER Cloud Platform 사용자 가이드(guide.ncloud-docs.com)를 Dify 지식 베이스에 넣는다. Firecrawl 없이.

방법: 사이트가 공개한 /llms.txt(전체 페이지 목록) → 페이지 주소 끝에 .md 를 붙여 마크다운 원문을 받음
     → Dify 지식 API /v1/datasets/{id}/document/create-by-text 로 문서 하나씩 넣음.

ncloud·dify.delvrio.com 둘 다 닿는 곳(회사 맥북)에서:
  cd ~/projects/dify && git pull
  python3 deploy/k3s/samples/ncp_guide_to_dify.py                 # 미리보기: 목록·페이지 수·샘플 3개만 받아 본다
  python3 deploy/k3s/samples/ncp_guide_to_dify.py --apply         # 전체 받기 + Dify 에 넣기(다시 돌리면 이미 넣은 문서는 건너뜀)

환경 변수(모두 선택):
  DIFY_DATASET_KEY  지식 API 키(Dify › 지식 › API 액세스). 없으면 화면에 안 보이게 묻는다
  DATASET_ID        넣을 지식 베이스 id. 없으면 DATASET_NAME 으로 찾고, 없으면 만든다
  DATASET_NAME      기본 "NCP 사용자 가이드"
  INCLUDE           넣을 주소 정규식(기본 = 한국어 일반 가이드 /docs/ 아래만)
  EXCLUDE           뺄 주소 정규식(기본 = 영문·일문 경로, 릴리스 노트)
  DELAY             페이지 사이 쉬는 초(기본 1.0, 상대 서버 배려)
  LIMIT             최대 페이지 수(기본 0 = 전부)
  UPDATE=1          이미 넣은 문서도 새 내용으로 바꿈
받은 원문은 ~/.cache/ncp-guide/ 에 남겨 다시 돌릴 때 다시 받지 않는다(지우면 새로 받음).
"""
import getpass
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SITE = os.getenv("NCP_SITE", "https://guide.ncloud-docs.com").rstrip("/")
DIFY = os.getenv("DIFY_URL", "https://dify.delvrio.com").rstrip("/") + "/v1"
INCLUDE = re.compile(os.getenv("INCLUDE", r"^https://guide\.ncloud-docs\.com/docs/"))
EXCLUDE = re.compile(os.getenv("EXCLUDE", r"/(en|ja|jp)/|[?&]lang=(en|ja)|release-?notes?"), re.I)
DELAY = float(os.getenv("DELAY", "1.0"))
LIMIT = int(os.getenv("LIMIT", "0"))
CACHE = os.path.expanduser(os.getenv("CACHE_DIR", "~/.cache/ncp-guide"))
APPLY = "--apply" in sys.argv
UA = {"User-Agent": "delvrio-docs-loader/1.0 (+internal knowledge base)"}

# 청킹: 마크다운 ## 제목 단위로 부모 조각, 그 안을 줄 단위 자식 조각으로(콘솔 메뉴 경로·단계 표 질문에 강함)
PROCESS_RULE = {"mode": "hierarchical", "rules": {
    "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": True}, {"id": "remove_urls_emails", "enabled": False}],
    "segmentation": {"separator": "\n## ", "max_tokens": 1500},
    "parent_mode": "paragraph",
    "subchunk_segmentation": {"separator": "\n", "max_tokens": 400}}}


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(10 * (i + 1))
                continue
            return e.code, ""
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                return 0, str(e)
            time.sleep(5 * (i + 1))
    return 429, ""


def dify(method, path, body=None):
    req = urllib.request.Request(DIFY + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


# 1) 목록
st, idx = get(SITE + "/llms.txt")
if st != 200:
    sys.exit(f"[실패] {SITE}/llms.txt 를 못 받았습니다(상태 {st}). 이 컴퓨터에서 사이트가 열리는지 확인하세요")
pages, seen = [], set()
for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", idx):
    url = url.split("#")[0]
    if url in seen or not INCLUDE.search(url) or EXCLUDE.search(url):
        continue
    seen.add(url)
    pages.append((title.strip(), url))
if LIMIT:
    pages = pages[:LIMIT]
print(f"llms.txt 링크 {len(re.findall(r'https?://', idx))}개 중 넣을 페이지 {len(pages)}개")
if not pages:
    sys.exit("[실패] 넣을 페이지가 없습니다. INCLUDE/EXCLUDE 를 확인하세요")


def md_url(url):
    p = urllib.parse.urlsplit(url)
    path = p.path.rstrip("/")
    return url if path.endswith(".md") else urllib.parse.urlunsplit((p.scheme, p.netloc, path + ".md", p.query, ""))


def fetch(url):
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + ".md")
    if os.path.exists(f):
        return 200, open(f, encoding="utf-8").read(), True
    st, text = get(md_url(url))
    if st == 200 and text.lstrip().startswith("<"):   # .md 대신 HTML 이 오면 실패로 본다
        st = -1
    if st == 200:
        open(f, "w", encoding="utf-8").write(text)
    return st, text, False


# 2) 미리보기
if not APPLY:
    for title, url in pages[:3]:
        st, text, _ = fetch(url)
        print(f"\n--- {title}  {md_url(url)}  상태 {st}, {len(text)}자 ---\n{text[:500]}")
        time.sleep(DELAY)
    est = len(pages) * (DELAY + 0.5) / 60
    print(f"\n미리보기 끝. 전체를 넣으려면 --apply (받기만 약 {est:.0f}분 + Dify 색인 시간)")
    sys.exit(0)

# 3) Dify 지식 베이스
KEY = (os.getenv("DIFY_DATASET_KEY") or getpass.getpass("Dify 지식 API 키(지식 › API 액세스): ")).strip()
ds_id = os.getenv("DATASET_ID", "").strip()
name = os.getenv("DATASET_NAME", "NCP 사용자 가이드")
if not ds_id:
    st, res = dify("GET", "/datasets?page=1&limit=100&keyword=" + urllib.parse.quote(name))
    if st != 200:
        sys.exit(f"[실패] 지식 목록 조회 {st}: {res}. API 키를 확인하세요")
    ds_id = next((d["id"] for d in res.get("data", []) if d.get("name") == name), "")
    if not ds_id:
        st, res = dify("POST", "/datasets", {"name": name, "description": f"{SITE} 사용자 가이드(llms.txt + .md)",
                                             "indexing_technique": "high_quality", "permission": "all_team_members"})
        if st not in (200, 201):
            sys.exit(f"[실패] 지식 베이스 만들기 {st}: {res}")
        ds_id = res["id"]
        print(f"지식 베이스 만듦: {name} ({ds_id})")
print(f"지식 베이스: {DIFY.removesuffix('/v1')}/datasets/{ds_id}/documents")

existing, page = {}, 1
while True:
    st, res = dify("GET", f"/datasets/{ds_id}/documents?page={page}&limit=100")
    if st != 200:
        sys.exit(f"[실패] 문서 목록 {st}: {res}")
    existing.update({d["name"]: d["id"] for d in res.get("data", [])})
    if not res.get("has_more"):
        break
    page += 1

done = skip = fail = 0
failed = []
for n, (title, url) in enumerate(pages, 1):
    doc_name = (title + " | " + urllib.parse.urlsplit(url).path)[:250]
    if doc_name in existing and os.getenv("UPDATE") != "1":
        skip += 1
        continue
    st, text, cached = fetch(url)
    if st != 200 or not text.strip():
        fail += 1
        failed.append((url, st))
        print(f"[{n}/{len(pages)}] 받기 실패 {st}: {url}")
        continue
    body = {"name": doc_name, "text": f"# {title}\n원문: {url}\n\n{text}", "doc_form": "hierarchical_model",
            "doc_language": "Korean", "indexing_technique": "high_quality", "process_rule": PROCESS_RULE}
    if doc_name in existing:
        st, res = dify("POST", f"/datasets/{ds_id}/documents/{existing[doc_name]}/update-by-text", body)
    else:
        st, res = dify("POST", f"/datasets/{ds_id}/document/create-by-text", body)
    if st != 200:
        fail += 1
        failed.append((url, f"dify {st}"))
        print(f"[{n}/{len(pages)}] Dify 넣기 실패 {st}: {json.dumps(res, ensure_ascii=False)[:300]}")
        if done == 0 and fail >= 3:
            sys.exit("[멈춤] 처음부터 계속 실패합니다. 위 오류(임베딩 모델 설정·키)를 먼저 고치세요")
        continue
    done += 1
    if n % 50 == 0 or n == len(pages):
        print(f"[{n}/{len(pages)}] 넣음 {done} · 건너뜀 {skip} · 실패 {fail}")
    if not cached:
        time.sleep(DELAY)

print(f"\n끝: 넣음 {done} · 이미 있음 {skip} · 실패 {fail}")
if failed:
    path = os.path.join(CACHE, "failed.txt")
    open(path, "w").write("\n".join(f"{s}\t{u}" for u, s in failed))
    print(f"실패 목록: {path} (다시 돌리면 실패한 것만 다시 시도)")
