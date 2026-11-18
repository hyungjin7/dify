#!/usr/bin/env python3
"""지식 출처 모음 상주 수집기(2026-11-19 사용자 결정: 늘 켜진 수집기 하나가 미리보기·지금 갱신·정기 갱신을 모두 맡는다).

출처 목록의 정본은 bbb agent 의 표(knowledge_sources, 화면 ccc › 지식 출처 모음). 이 수집기는:
  1) 받기 줄(매 POLL 초): agent POST /internal/knowledge-sources/claim → 할 일 하나(sync|delete)
       sync   = tech_docs_to_dify.sync() 로 받아 Dify 지식(출처 1개 = 지식 1개)에 넣고 바뀐 것만 갱신
       delete = 그 출처의 Dify 지식을 지운다(관리자가 화면에서 삭제를 확정한 것)
     → POST /internal/knowledge-sources/<id>/report 로 결과(쪽 수·추가·교체·삭제·실패)를 알린다. 한 번에 하나씩.
  2) 미리보기 줄(매 3초, 별도 스레드 — 긴 수집 중에도 마법사가 기다리지 않게):
     POST /internal/knowledge-sources/preview/claim → 주소를 살펴 연결 방법을 고르고 앞의 몇 쪽을 받아 본다
       순서: GitHub 저장소 주소 → llms.txt → 문서의 GitHub 원문 링크(Edit this page) → 사이트 따라가기
       (공식 MCP 는 agent 가 알려진 목록으로 먼저 판단한다 — "이미 AI 창구가 있으면 가져다 쓴다")
     → POST /internal/knowledge-sources/preview/<id>/result
정기 갱신 시각·지금 갱신 요청·멈춤은 모두 agent 가 판단한다(수집기는 시키는 것만 한다).

환경 변수: AGENT_URL(기본 http://agent-service.bbb-namespace.svc.cluster.local:8000),
  COLLECTOR_TOKEN(= bbb Secret knowledge-collector-token 과 같은 값, Secret dify/knowledge-collector 의 token),
  DIFY_DATASET_KEY(지식 API 키, Secret ncp-guide-sync 의 key), DIFY_URL, STATE_DIR, POLL_SECONDS(기본 60)
"""
import html
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tech_docs_to_dify as td   # noqa: E402

AGENT = os.getenv("AGENT_URL", "http://agent-service.bbb-namespace.svc.cluster.local:8000").rstrip("/")
TOKEN = os.getenv("COLLECTOR_TOKEN", "").strip()
POLL = float(os.getenv("POLL_SECONDS", "60"))
PREVIEW_POLL = float(os.getenv("PREVIEW_POLL_SECONDS", "3"))
SAMPLES = 3
UA = td.UA


def log(msg):
    print(f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}", flush=True)


def agent(path, body=None):
    req = urllib.request.Request(AGENT + "/internal/knowledge-sources" + path, method="POST",
                                 data=json.dumps(body or {}, ensure_ascii=False, default=str).encode(),
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


# ---------- 받기·지우기 ----------
def run_sync(src):
    started = datetime.now(timezone.utc).isoformat()
    log(f"[받기] {src['id']} ({src['name']}, {src['kind']})")
    try:
        c = td.sync(src["id"], src) or {}
        fail = c.get("fail", 0)
        ok = not (fail and not (c.get("added") or c.get("updated") or c.get("same")))
        msg = c.get("message") or ("" if ok else f"실패 {fail}건")
    except SystemExit as e:          # tech_docs_to_dify 는 멈춰야 할 때 sys.exit(문구)
        c, ok, msg = {}, False, str(e.code)[:2000]
    except Exception as e:           # noqa: BLE001
        c, ok, msg = {}, False, f"{type(e).__name__}: {e}"[:2000]
        traceback.print_exc()
    return {"action": "sync", "ok": ok, "started_at": started, "pages": c.get("pages"), "added": c.get("added", 0),
            "updated": c.get("updated", 0), "same": c.get("same", 0), "removed": c.get("removed", 0),
            "failed": c.get("fail", 0), "dataset_id": c.get("dataset_id"), "message": msg}


def run_delete(src):
    started = datetime.now(timezone.utc).isoformat()
    log(f"[지우기] {src['id']} ({src['dataset']})")
    ds_id = src.get("dataset_id")
    if not ds_id:
        found = td.find_dataset(src["dataset"])
        ds_id = (found or {}).get("id")
    st = 404
    if ds_id:
        st, _ = td.dify("DELETE", f"/datasets/{ds_id}")
    try:
        os.remove(os.path.join(td.STATE_DIR, f"{src['id']}.json"))
    except OSError:
        pass
    ok = st in (200, 204, 404)
    return {"action": "delete", "ok": ok, "started_at": started,
            "message": "" if ok else f"Dify 지식 지우기 실패 {st}"}


def sync_loop():
    while True:
        try:
            job = agent("/claim").get("job")
        except Exception as e:   # noqa: BLE001 — agent 가 잠깐 내려가도 수집기는 산다
            log(f"[대기] agent 연결 실패: {e!r}")
            time.sleep(POLL)
            continue
        if not job:
            time.sleep(POLL)
            continue
        src = job["source"]
        res = run_delete(src) if job["action"] == "delete" else run_sync(src)
        for i in range(5):
            try:
                agent(f"/{urllib.parse.quote(src['id'])}/report", res)
                break
            except Exception as e:   # noqa: BLE001
                log(f"[보고 실패] {src['id']}: {e!r}")
                time.sleep(10 * (i + 1))
        log(f"[끝] {src['id']} ok={res['ok']} {res.get('message') or ''}")


# ---------- 미리보기(연결 방법 찾기) ----------
GH_REPO = re.compile(r"^https?://github\.com/([\w.-]+)/([\w.-]+)", re.I)
GH_LINK = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/(?:edit|blob|tree)/([\w.\-]+)/([^\"'#?\s<>]+)", re.I)
DOC_DIRS = ("docs", "Documentation", "doc", "content/en/docs", "content/docs", "content", "documentation")
LICENSES = (("Creative Commons Attribution-ShareAlike", "CC BY-SA"), ("Creative Commons Attribution-NonCommercial", "CC BY-NC"),
            ("Creative Commons Attribution", "CC BY"), ("CC BY-SA", "CC BY-SA"), ("CC BY-NC", "CC BY-NC"), ("CC BY", "CC BY"),
            ("Apache License", "Apache 2.0"), ("MIT License", "MIT"), ("GNU Free Documentation", "GFDL"))


def gh_api(path):
    st, body = td.get("https://api.github.com" + path, tries=1)
    return json.loads(body) if st == 200 and body else None


def gh_plan(repo, ref=None, want_dir=None, base=None):
    """GitHub 저장소 → (설정, 쪽 수, 첫 파일들, 라이선스). 트리 API 로 세고 앞의 몇 개만 raw 로 받는다(묶음 전체는 안 받음)."""
    info = gh_api(f"/repos/{repo}") or {}
    ref = ref or info.get("default_branch") or "main"
    tree = gh_api(f"/repos/{repo}/git/trees/{urllib.parse.quote(ref)}?recursive=1") or {}
    files = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob" and t["path"].endswith((".md", ".mdx", ".rst"))]
    dirs = [want_dir] if want_dir else list(DOC_DIRS)
    for d in dirs:
        inside = [f for f in files if f.startswith(d.rstrip("/") + "/")]
        if inside:
            d = d.rstrip("/")
            cfg = {"repo": repo, "ref": [ref], "dirs": [d],
                   "base": base or f"https://github.com/{repo}/blob/{ref}/{d}/"}
            if not base:
                cfg["suffix"] = ".md"
            lic = ((info.get("license") or {}).get("spdx_id") or "").replace("NOASSERTION", "")
            return cfg, len(inside), inside[:SAMPLES], lic
    return None, 0, [], ""


def gh_samples(repo, ref, paths):
    out = []
    for p in paths:
        st, raw = td.get(f"https://raw.githubusercontent.com/{repo}/{ref}/{p}", tries=1)
        if st != 200:
            continue
        title, text = td.clean_rst(raw) if p.endswith(".rst") else td.clean_md(raw)
        out.append({"title": title or p.rsplit("/", 1)[-1], "url": f"https://github.com/{repo}/blob/{ref}/{p}",
                    "chars": len(text), "head": text[:300]})
    return out


def kind_samples(kind, cfg, name):
    """llms·crawl 등: 앞의 몇 쪽만 실제로 받아 본다."""
    pages = td.KINDS[kind]({**cfg, "name": name, "max": SAMPLES})
    return [{"title": t, "url": u, "chars": len(x), "head": x[:300]} for t, u, x in pages[:SAMPLES]]


def license_in(text):
    for needle, label in LICENSES:
        if needle.lower() in text.lower():
            return label + " (사이트 안내에서 찾음, 확인 필요)"
    return ""


def korean(samples):
    text = " ".join(s["head"] for s in samples)
    return len(re.findall(r"[가-힣]", text)) > len(text) * 0.15


def detect(url):
    checks, rec, pages, samples, lic, limited = [], None, 0, [], "", False
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"

    m = GH_REPO.match(url)
    if m:
        repo = f"{m.group(1)}/{m.group(2).removesuffix('.git')}"
        cfg, pages, first, lic = gh_plan(repo)
        checks.append({"label": "GitHub 원문", "ok": bool(cfg), "note": f"{repo} · {cfg['dirs'][0]}/ ({pages}개 파일)" if cfg else f"{repo} 에 문서 폴더(docs 등)가 없음"})
        if cfg:
            rec, samples = {"method": "git", "config": cfg}, gh_samples(repo, cfg["ref"][0], first)
    if not rec:
        st, idx = td.get(origin + "/llms.txt", tries=1)
        links = re.findall(r"\[[^\]]+\]\((https?://[^)\s]+)\)", idx) if st == 200 else []
        links = [x for x in links if urllib.parse.urlsplit(x).netloc == parts.netloc]
        checks.append({"label": "llms.txt", "ok": bool(links), "note": f"{origin}/llms.txt · 링크 {len(links)}개" if links else f"{origin}/llms.txt 없음"})
        if links:
            cfg = {"site": origin, "max": 2000}
            rec, pages, limited = {"method": "llms", "config": cfg}, len(set(links)), False
            samples = kind_samples("llms", cfg, parts.netloc)
    page_html = ""
    if not rec:
        st, page_html = td.get(url, tries=1)
        page_html = page_html if st == 200 else ""
        m = GH_LINK.search(page_html)
        if m:
            repo, ref, path = f"{m.group(1)}/{m.group(2)}", m.group(3), m.group(4)
            top = next((d for d in DOC_DIRS if path.startswith(d + "/")), path.split("/", 1)[0])
            base = url if url.endswith("/") else url.rsplit("/", 1)[0] + "/"
            cfg, pages, first, lic = gh_plan(repo, ref, top, base)
            checks.append({"label": "GitHub 원문", "ok": bool(cfg),
                           "note": f"{repo}@{ref} · {top}/ ({pages}개 파일, 주소 대응은 승인 때 확인)" if cfg else f"{repo} 에서 문서 폴더를 못 찾음"})
            if cfg:
                rec, samples = {"method": "git", "config": cfg}, gh_samples(repo, ref, first)
        else:
            checks.append({"label": "GitHub 원문", "ok": False, "note": "문서에 원문 링크(Edit this page)가 없음"})
    if not rec:
        st, sm = td.get(origin + "/sitemap.xml", tries=1)
        locs = [x for x in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm or "") if x.startswith(url.rsplit("/", 1)[0])] if st == 200 else []
        checks.append({"label": "sitemap.xml", "ok": bool(locs) if st == 200 else False,
                       "note": f"이 주소 아래 {len(locs)}쪽" if locs else "없음 또는 이 주소 아래가 없음"})
        prefix = url if url.endswith("/") else url.rsplit("/", 1)[0] + "/"
        cfg = {"start": [url], "prefix": [prefix], "max": min(max(len(locs), 300), 1500)}
        try:
            samples = kind_samples("crawl", cfg, parts.netloc)
        except Exception as e:   # noqa: BLE001
            samples = []
            checks.append({"label": "사이트 따라가기", "ok": False, "note": f"{type(e).__name__}"})
        if samples:
            checks.append({"label": "사이트 따라가기", "ok": True, "note": f"{prefix} 아래 · 최대 {cfg['max']}쪽"})
            rec, pages, limited = {"method": "crawl", "config": cfg}, len(locs) or len(samples), not locs
        else:
            checks.append({"label": "사이트 따라가기", "ok": False, "note": "본문을 읽지 못함(자바스크립트로 그리는 화면일 수 있음)"})
    if rec:
        rec["lang"] = "Korean" if korean(samples) else "English"
    if not lic:
        if not page_html:
            st, page_html = td.get(url, tries=1)
        lic = license_in(re.sub(r"<[^>]+>", " ", html.unescape(page_html or ""))[-20000:])
    elif "확인" not in lic:
        lic += " (저장소 라이선스, 문서는 다를 수 있음)"
    return {"checks": checks, "recommended": rec, "pages": pages, "limited": limited, "samples": samples, "license_hint": lic}


def preview_loop():
    while True:
        try:
            job = agent("/preview/claim").get("job")
        except Exception:   # noqa: BLE001
            time.sleep(15)
            continue
        if not job:
            time.sleep(PREVIEW_POLL)
            continue
        log(f"[미리보기] {job['url']}")
        try:
            res = detect(job["url"])
        except Exception as e:   # noqa: BLE001
            traceback.print_exc()
            res = {"error": f"살펴보기 실패: {type(e).__name__}: {e}"[:500], "checks": []}
        try:
            agent(f"/preview/{job['id']}/result", res)
        except Exception as e:   # noqa: BLE001
            log(f"[미리보기 보고 실패] {e!r}")


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("[실패] COLLECTOR_TOKEN 이 없습니다(Secret dify/knowledge-collector). samples/knowledge_collector_setup.py 를 먼저 돌리세요")
    td.KEY = os.getenv("DIFY_DATASET_KEY", "").strip()
    if not td.KEY:
        sys.exit("[실패] 지식 API 키가 없습니다(Secret dify/ncp-guide-sync 의 key). 먼저 samples/ncp_guide_setup.py")
    td.APPLY = True
    os.makedirs(td.STATE_DIR, exist_ok=True)
    threading.Thread(target=preview_loop, daemon=True, name="preview").start()
    log(f"수집기 시작: agent={AGENT} · 받기 {POLL:.0f}초마다 · 미리보기 {PREVIEW_POLL:.0f}초마다")
    sync_loop()
