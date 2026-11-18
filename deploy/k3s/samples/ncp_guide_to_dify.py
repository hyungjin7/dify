#!/usr/bin/env python3
"""NAVER Cloud Platform 사용자 가이드(guide.ncloud-docs.com)를 Dify 전용 지식 베이스에 넣고, 바뀐 것만 갱신한다.

방법(Firecrawl 없음): /llms.txt(전체 페이지 목록) → 페이지 주소 + .md(마크다운 원문)
     → Dify 지식 API create-by-text / update-by-text / 삭제. 기존 지식(rag·다른 지식 베이스)과 섞이지 않는다.

갱신 규칙(상태 파일 STATE_DIR/state.json = 주소 → 문서 id·원문 지문·ETag):
  새 주소 → 추가, 원문 지문이 바뀜 → 그 문서만 교체, llms.txt 에서 사라진 주소 → Dify 에서 삭제.
  사이트가 ETag/Last-Modified 를 주면 조건부 요청(304 = 안 바뀜)으로 받는 시간도 줄인다.

어디서 돌리나:
  - 정기 갱신: Kubernetes CronJob dify/ncp-guide-sync(deploy/k3s/ncp-guide-sync.yaml, 매주 일요일 03:00 KST, 상태는 nfs-hdd)
  - 손으로: python3 deploy/k3s/samples/ncp_guide_to_dify.py            # 미리보기(목록·샘플 3개)
            python3 deploy/k3s/samples/ncp_guide_to_dify.py --apply    # 적재/갱신
환경 변수:
  DIFY_DATASET_KEY  지식 API 키(없으면 묻는다)   DATASET_ID  지식 베이스 id(없으면 DATASET_NAME 으로 찾고 없으면 만든다)
  DATASET_NAME      기본 "NCP 사용자 가이드"     DIFY_URL    기본 https://dify.delvrio.com (CronJob 은 http://dify-api:5001)
  INCLUDE / EXCLUDE 넣을·뺄 주소 정규식(기본 = 한국어 일반 가이드 /docs/ 만)
  DELAY             페이지 사이 쉬는 초(기본 1.0)   LIMIT  최대 페이지 수(0 = 전부)
  STATE_DIR         상태·원문 보관(기본 ~/.cache/ncp-guide)
  MIN_KEEP_RATIO    목록이 지난번의 이 비율 밑으로 줄면 삭제를 하지 않고 멈춘다(기본 0.8, 사이트 장애 대비)
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
STATE_DIR = os.path.expanduser(os.getenv("STATE_DIR", "~/.cache/ncp-guide"))
MIN_KEEP = float(os.getenv("MIN_KEEP_RATIO", "0.8"))
APPLY = "--apply" in sys.argv
UA = {"User-Agent": "delvrio-docs-loader/1.0 (internal knowledge base; 1 req/s)"}

# 청킹: ## 제목 단위 부모 조각 + 그 안의 줄 단위 자식 조각(콘솔 메뉴 경로·단계 표 질문에 강함)
PROCESS_RULE = {"mode": "hierarchical", "rules": {
    "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": True}, {"id": "remove_urls_emails", "enabled": False}],
    "segmentation": {"separator": "\n## ", "max_tokens": 1500},
    "parent_mode": "paragraph",
    "subchunk_segmentation": {"separator": "\n", "max_tokens": 400}}}


def log(msg):
    print(msg, flush=True)


def get(url, headers=None, tries=3):
    """(상태, 본문, 응답 헤더). 429·5xx 는 쉬었다 다시."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, "", dict(e.headers)
            if e.code == 429 or e.code >= 500:
                time.sleep(15 * (i + 1))
                continue
            return e.code, "", {}
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                return 0, str(e), {}
            time.sleep(5 * (i + 1))
    return 429, "", {}


def dify(method, path, body=None):
    req = urllib.request.Request(DIFY + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}


def md_url(url):
    p = urllib.parse.urlsplit(url)
    path = p.path.rstrip("/")
    return url if path.endswith(".md") else urllib.parse.urlunsplit((p.scheme, p.netloc, path + ".md", p.query, ""))


def doc_name(title, url):
    return (title + " | " + urllib.parse.urlsplit(url).path)[:250]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# 1) 목록
st, idx, _ = get(SITE + "/llms.txt")
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
log(f"llms.txt 링크 {len(re.findall(r'https?://', idx))}개 중 넣을 페이지 {len(pages)}개")
if not pages:
    sys.exit("[실패] 넣을 페이지가 없습니다. INCLUDE/EXCLUDE 를 확인하세요")

# 2) 미리보기
if not APPLY:
    for title, url in pages[:3]:
        st, text, h = get(md_url(url))
        log(f"\n--- {title}  {md_url(url)}  상태 {st}, {len(text)}자, ETag={'있음' if h.get('ETag') else '없음'} ---\n{text[:500]}")
        time.sleep(DELAY)
    log(f"\n미리보기 끝. 적재하려면 --apply (처음엔 약 {len(pages) * (DELAY + 0.5) / 60:.0f}분 + Dify 색인 시간)")
    sys.exit(0)

# 3) 지식 베이스
KEY = os.getenv("DIFY_DATASET_KEY", "").strip()
if not KEY and sys.stdin.isatty():   # Job(터미널 없음)에서는 묻지 않는다
    KEY = getpass.getpass("Dify 지식 API 키: ").strip()
if not KEY:
    sys.exit("[실패] 지식 API 키가 없습니다. Secret dify/ncp-guide-sync 가 없으니 먼저 "
             "python3 deploy/k3s/samples/ncp_guide_setup.py 를 끝까지 돌리세요")
ds_id = os.getenv("DATASET_ID", "").strip()
name = os.getenv("DATASET_NAME", "NCP 사용자 가이드")
if not ds_id:
    st, res = dify("GET", "/datasets?page=1&limit=100&keyword=" + urllib.parse.quote(name))
    if st != 200:
        sys.exit(f"[실패] 지식 목록 조회 {st}: {res}. API 키를 확인하세요")
    ds_id = next((d["id"] for d in res.get("data", []) if d.get("name") == name), "")
    if not ds_id:
        st, res = dify("POST", "/datasets", {"name": name, "description": f"{SITE} 사용자 가이드(llms.txt + .md, 주 1회 갱신)",
                                             "indexing_technique": "high_quality", "permission": "only_me"})
        if st not in (200, 201):
            sys.exit(f"[실패] 지식 베이스 만들기 {st}: {res}")
        ds_id = res["id"]
        log(f"지식 베이스 만듦: {name} ({ds_id})")
log(f"지식 베이스 {ds_id}")

# Dify 에 실제로 있는 문서(이름 → id). 상태 파일이 없어도(처음·PC 바뀜) 이름으로 이어 붙는다.
existing, page = {}, 1
while True:
    st, res = dify("GET", f"/datasets/{ds_id}/documents?page={page}&limit=100")
    if st != 200:
        sys.exit(f"[실패] 문서 목록 {st}: {res}")
    existing.update({d["name"]: d["id"] for d in res.get("data", [])})
    if not res.get("has_more"):
        break
    page += 1

os.makedirs(STATE_DIR, exist_ok=True)
state_path = os.path.join(STATE_DIR, "state.json")
try:
    state = json.load(open(state_path, encoding="utf-8"))
except (OSError, ValueError):
    state = {}
state = state if state.get("dataset_id") == ds_id else {"dataset_id": ds_id, "docs": {}}
docs = state.setdefault("docs", {})


def save_state():
    tmp = state_path + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, state_path)


# 4) 추가·교체
added = updated = same = fail = 0
failed = []
for n, (title, url) in enumerate(pages, 1):
    name_ = doc_name(title, url)
    rec = docs.get(url) or {}
    doc_id = rec.get("doc_id") or existing.get(name_)
    cond = {}
    if doc_id and rec.get("etag"):
        cond["If-None-Match"] = rec["etag"]
    if doc_id and rec.get("modified"):
        cond["If-Modified-Since"] = rec["modified"]
    st, text, h = get(md_url(url), cond)
    if st == 304:
        same += 1
        continue
    if st != 200 or not text.strip() or text.lstrip().startswith("<"):   # HTML 이 오면 .md 가 없는 페이지
        fail += 1
        failed.append(f"{st}\t{url}")
        log(f"[{n}/{len(pages)}] 받기 실패 {st}: {url}")
        time.sleep(DELAY)
        continue
    digest = sha(text)
    if doc_id and rec.get("sha") == digest:
        same += 1
        docs[url] = {**rec, "doc_id": doc_id, "etag": h.get("ETag", ""), "modified": h.get("Last-Modified", "")}
        time.sleep(DELAY)
        continue
    body = {"name": name_, "text": f"# {title}\n원문: {url}\n\n{text}", "doc_form": "hierarchical_model",
            "doc_language": "Korean", "indexing_technique": "high_quality", "process_rule": PROCESS_RULE}
    if doc_id:
        st, res = dify("POST", f"/datasets/{ds_id}/documents/{doc_id}/update-by-text", body)
        if st == 404:   # Dify 에서 지운 문서 → 새로
            doc_id = None
    if not doc_id:
        st, res = dify("POST", f"/datasets/{ds_id}/document/create-by-text", body)
    if st != 200:
        fail += 1
        failed.append(f"dify {st}\t{url}")
        log(f"[{n}/{len(pages)}] Dify 넣기 실패 {st}: {json.dumps(res, ensure_ascii=False)[:300]}")
        if added + updated == 0 and fail >= 3:
            save_state()
            sys.exit("[멈춤] 처음부터 계속 실패합니다. 위 오류(임베딩 모델 설정·키)를 먼저 고치세요")
        continue
    new_id = ((res or {}).get("document") or {}).get("id") or doc_id
    if rec.get("doc_id") or existing.get(name_):
        updated += 1
    else:
        added += 1
    docs[url] = {"doc_id": new_id, "name": name_, "sha": digest, "etag": h.get("ETag", ""),
                 "modified": h.get("Last-Modified", "")}
    if (added + updated) % 20 == 0:
        save_state()
        log(f"[{n}/{len(pages)}] 추가 {added} · 교체 {updated} · 그대로 {same} · 실패 {fail}")
    time.sleep(DELAY)
save_state()

# 5) 사라진 페이지 삭제(목록이 갑자기 크게 줄면 사이트 문제로 보고 지우지 않는다)
gone = [u for u in docs if u not in seen]
removed = 0
if gone and not LIMIT:
    if len(pages) < MIN_KEEP * max(1, len(docs)):
        log(f"[주의] 목록이 지난번 {len(docs)}개 → 이번 {len(pages)}개로 크게 줄어 삭제는 건너뜁니다")
    else:
        for u in gone:
            st, _ = dify("DELETE", f"/datasets/{ds_id}/documents/{docs[u]['doc_id']}")
            if st in (200, 204, 404):
                removed += 1
                docs.pop(u)
        save_state()

log(f"\n끝: 추가 {added} · 교체 {updated} · 그대로 {same} · 삭제 {removed} · 실패 {fail}")
if failed:
    open(os.path.join(STATE_DIR, "failed.txt"), "w").write("\n".join(failed))
    log(f"실패 목록: {os.path.join(STATE_DIR, 'failed.txt')} (다음 실행 때 다시 시도)")
