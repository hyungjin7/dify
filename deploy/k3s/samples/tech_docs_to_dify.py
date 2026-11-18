#!/usr/bin/env python3
"""인프라 기술 문서(Kubernetes·PostgreSQL·Traefik·Argo CD·Helm)를 Dify 지식 베이스에 넣고 바뀐 것만 갱신한다.

NCP 가이드(ncp_guide_to_dify.py)와 같은 방식이고(2026-11-18 사용자 요청), 문서마다 지식 베이스를 따로 둔다:
  "기술문서 · Kubernetes" / "· PostgreSQL" / "· Traefik" / "· Argo CD" / "· Helm"
  → 한 문서만 골라 묻거나(정확), 다섯 개를 함께 물을 수 있다. 다른 지식(rag·NCP)과 섞이지 않는다.

가져오는 방법(사이트를 페이지마다 긁지 않는다):
  - Kubernetes·Argo CD·Helm·Traefik: 문서 원문(마크다운)이 GitHub 저장소에 있다 → 저장소 묶음(tar.gz) 1번 받기
  - PostgreSQL: 원문이 SGML 이라 공식 사이트 /docs/current/ 의 HTML 페이지를 받아 글자만 뽑는다(릴리스 노트 제외)

갱신 규칙(상태 STATE_DIR/<문서>.json = 주소 → 문서 id·원문 지문): 새 주소 → 추가, 원문이 바뀜 → 그 문서만 교체,
목록에서 사라짐 → 삭제(목록이 지난번의 MIN_KEEP_RATIO 밑으로 줄면 사이트 문제로 보고 지우지 않는다).
진행 기록은 문서 하나 넣을 때마다 저장한다(중간에 끊어도 다시 돌리면 이어서, 중복 없음).

  python3 deploy/k3s/samples/tech_docs_to_dify.py                       # 미리보기(문서별 페이지 수·샘플)
  python3 deploy/k3s/samples/tech_docs_to_dify.py --apply               # 다섯 개 모두 적재/갱신
  SOURCES=helm,traefik python3 deploy/k3s/samples/tech_docs_to_dify.py --apply   # 일부만
환경 변수: DIFY_DATASET_KEY(지식 API 키 = Secret dify/ncp-guide-sync 의 key), DIFY_URL, STATE_DIR(기본 ~/.cache/tech-docs),
  SOURCES(기본 전부), DELAY(Dify 넣기 사이 초, 기본 0.2), PAGE_DELAY(PostgreSQL 페이지 사이 초, 기본 0.5), LIMIT(문서별 최대, 0=전부)
  EMBEDDING_MODEL / EMBEDDING_PROVIDER(새 지식 베이스의 임베딩 모델, 없으면 "NCP 사용자 가이드" 와 같은 모델)
"""
import getpass
import hashlib
import html
import json
import os
import posixpath
import re
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

DIFY = os.getenv("DIFY_URL", "https://dify.delvrio.com").rstrip("/") + "/v1"
STATE_DIR = os.path.expanduser(os.getenv("STATE_DIR", "~/.cache/tech-docs"))
DELAY = float(os.getenv("DELAY", "0.2"))
PAGE_DELAY = float(os.getenv("PAGE_DELAY", "0.5"))
LIMIT = int(os.getenv("LIMIT", "0"))
MIN_KEEP = float(os.getenv("MIN_KEEP_RATIO", "0.8"))
APPLY = "--apply" in sys.argv
UA = {"User-Agent": "delvrio-docs-loader/1.0 (internal knowledge base)"}

# 문서 목록. kind=git: 저장소 묶음 안의 dirs 아래 .md/.mdx → base + 경로. kind=html: 같은 폴더의 *.html 을 따라간다.
SOURCES = {
    "kubernetes": {"name": "Kubernetes", "kind": "git", "repo": "kubernetes/website", "ref": "main",
                   "dirs": ["content/en/docs"], "base": "https://kubernetes.io/docs/",
                   "exclude": r"^(reference/(kubernetes-api|generated|config-api|command-line-tools-reference)|contribute|test)/"},
    "argocd": {"name": "Argo CD", "kind": "git", "repo": "argoproj/argo-cd", "ref": "stable",
               "dirs": ["docs"], "base": "https://argo-cd.readthedocs.io/en/stable/",
               "exclude": r"^(proposals|snippets|assets)/"},
    "helm": {"name": "Helm", "kind": "git", "repo": "helm/helm-www", "ref": "main",
             "dirs": ["docs", "content/en/docs"], "base": "https://helm.sh/docs/",
             "exclude": r"^(blog|community)/"},
    "traefik": {"name": "Traefik", "kind": "git", "repo": "traefik/traefik", "ref": "master",
                "dirs": ["docs/content"], "base": "https://doc.traefik.io/traefik/",
                "exclude": r"^(assets|includes|overrides)/"},
    "postgresql": {"name": "PostgreSQL", "kind": "html", "base": "https://www.postgresql.org/docs/current/",
                   "start": "index.html", "exclude": r"^release-\d"},
}
DATASET_PREFIX = "기술문서 · "

PROCESS_RULE = {"mode": "hierarchical", "rules": {
    "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": True}, {"id": "remove_urls_emails", "enabled": False}],
    "segmentation": {"separator": "\n## ", "max_tokens": 1500},
    "parent_mode": "paragraph",
    "subchunk_segmentation": {"separator": "\n", "max_tokens": 400}}}


def log(msg):
    print(msg, flush=True)


def get(url, raw=False, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
                body = r.read()
                return r.status, (body if raw else body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(15 * (i + 1))
                continue
            return e.code, b"" if raw else ""
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                return 0, b"" if raw else str(e)
            time.sleep(5 * (i + 1))
    return 429, b"" if raw else ""


# ---------- 마크다운 정리(Hugo·MkDocs·Docusaurus 문법 걷어내기) ----------
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def clean_md(text):
    title = ""
    m = FRONT.match(text)
    if m:
        t = re.search(r"^title:\s*(.+)$", m.group(1), re.M)
        title = t.group(1).strip().strip("'\"") if t else ""
        text = text[m.end():]
    text = re.sub(r"^(import|export) .*$", "", text, flags=re.M)                 # Docusaurus mdx
    text = re.sub(r"\{\{[<%]\s*/?\s*(glossary_tooltip[^>%]*text=\"([^\"]*)\"[^>%]*)[>%]\}\}", r"\2", text)
    text = re.sub(r"\{\{[<%].*?[>%]\}\}", "", text, flags=re.S)                  # Hugo shortcode 태그
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not title:
        h = re.search(r"^#\s+(.+)$", text, re.M)
        title = h.group(1).strip() if h else ""
    return title, text


def git_pages(src):
    """저장소 묶음을 흘려 읽는다(통째로 메모리에 올리지 않음 — kubernetes/website 는 수백 MB)."""
    url = f"https://codeload.github.com/{src['repo']}/tar.gz/refs/heads/{src['ref']}"
    log(f"  저장소 묶음 받는 중: {src['repo']}@{src['ref']}")
    exclude = re.compile(src["exclude"]) if src.get("exclude") else None
    found = {d: [] for d in src["dirs"]}
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, \
                    tarfile.open(fileobj=r, mode="r|gz") as tar:
                for m in tar:
                    if not m.isfile() or not m.name.endswith((".md", ".mdx")):
                        continue
                    inner = m.name.split("/", 1)[-1]
                    d = next((d for d in src["dirs"] if inner.startswith(d + "/")), None)
                    if not d:
                        continue
                    rel = inner[len(d) + 1:]
                    if exclude and exclude.search(rel):
                        continue
                    title, text = clean_md(tar.extractfile(m).read().decode("utf-8", "replace"))
                    if len(text) < 80:
                        continue
                    path = re.sub(r"\.mdx?$", "", rel)
                    path = re.sub(r"(^|/)(_?index|README)$", r"\1", path, flags=re.I)
                    page_url = src["base"] + (path + "/" if path and not path.endswith("/") else path)
                    found[d].append((title or posixpath.basename(path.rstrip("/")) or src["name"], page_url, text))
            break
        except (urllib.error.URLError, TimeoutError, tarfile.TarError, OSError) as e:
            if i == 2:
                raise RuntimeError(f"{url} 받기 실패: {e!r}")
            time.sleep(10 * (i + 1))
    return next((found[d] for d in src["dirs"] if found[d]), [])   # 먼저 적힌 폴더 우선


# ---------- PostgreSQL HTML → 글자 ----------
class DocText(HTMLParser):
    BLOCK = {"p", "div", "li", "tr", "dt", "dd", "table", "br", "ul", "ol", "dl", "blockquote"}
    SKIP_CLASS = ("navheader", "navfooter", "toc")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.title, self.links = [], "", []
        self.depth = 0          # docContent 안 깊이(0 = 밖)
        self.skip = 0           # 건너뛸 블록 깊이
        self.pre = False
        self.in_title = False
        self.head = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "title":
            self.in_title = True
        if tag == "div" and a.get("id") == "docContent":
            self.depth = 1
            return
        if not self.depth:
            return
        if tag == "div":
            self.depth += 1
            if self.skip or any(c in (a.get("class") or "") for c in self.SKIP_CLASS):
                self.skip += 1
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n" + "#" * max(2, int(tag[1])) + " ")
        elif tag == "pre":
            self.pre = True
            self.out.append("\n```\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag in self.BLOCK:
            self.out.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if not self.depth:
            return
        if tag == "div":
            if self.skip:
                self.skip -= 1
            self.depth -= 1
            return
        if self.skip:
            return
        if tag == "pre":
            self.pre = False
            self.out.append("\n```\n")
        elif tag in ("h1", "h2", "h3", "h4", "p"):
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.head += data
        if self.depth and not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self):
        t = "".join(self.out)
        t = re.sub(r"[ \t]+\n", "\n", t)
        return re.sub(r"\n{3,}", "\n\n", t).strip()


def html_pages(src):
    base, exclude = src["base"], re.compile(src["exclude"]) if src.get("exclude") else None
    queue, seen, pages = [src["start"]], {src["start"]}, []
    while queue and (not LIMIT or len(pages) < LIMIT):
        name = queue.pop(0)
        st, body = get(base + name)
        time.sleep(PAGE_DELAY)
        if st != 200:
            log(f"  받기 실패 {st}: {base + name}")
            continue
        p = DocText()
        p.feed(body)
        for href in p.links:
            h = href.split("#")[0]
            if re.fullmatch(r"[a-z0-9][a-z0-9._-]*\.html", h or "") and h not in seen and not (exclude and exclude.search(h)):
                seen.add(h)
                queue.append(h)
        text = p.text()
        if not text:   # 본문 칸(docContent)을 못 찾으면 태그만 걷어낸다
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", body, flags=re.S))).strip()
        title = html.unescape(p.head).split(": ")[-1].strip()   # "PostgreSQL: Documentation: 17: 5.1. Table Basics"
        if len(text) >= 80:
            pages.append((title or name, base + name, text))
        if len(pages) % 100 == 0 and pages:
            log(f"  PostgreSQL 페이지 {len(pages)}개 받음…")
    return pages


# ---------- Dify ----------
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


def find_dataset(name):
    st, res = dify("GET", "/datasets?page=1&limit=100&keyword=" + urllib.parse.quote(name))
    if st != 200:
        sys.exit(f"[실패] 지식 목록 조회 {st}: {res}. API 키를 확인하세요")
    return next((d for d in res.get("data", []) if d.get("name") == name), None)


def embedding():
    if os.getenv("EMBEDDING_MODEL"):
        return os.getenv("EMBEDDING_MODEL"), os.getenv("EMBEDDING_PROVIDER", "")
    ref = find_dataset(os.getenv("REF_DATASET", "NCP 사용자 가이드")) or {}
    if not ref.get("embedding_model"):
        sys.exit("[실패] 임베딩 모델을 정하지 못했습니다. EMBEDDING_MODEL·EMBEDDING_PROVIDER 를 주거나 NCP 가이드 준비를 먼저 하세요")
    return ref["embedding_model"], ref.get("embedding_model_provider", "")


def doc_name(title, url):
    name = f"{title} | {url}"
    return name if len(name) <= 250 else f"{title[:250 - len(url) - 3]} | {url}"


def sync(key, src):
    pages = (git_pages if src["kind"] == "git" else html_pages)(src)
    pages = list({u: (t, u, x) for t, u, x in pages}.values())
    if LIMIT:
        pages = pages[:LIMIT]
    log(f"[{src['name']}] 넣을 페이지 {len(pages)}개")
    if not pages:
        log(f"[{src['name']}] 페이지가 없어 건너뜁니다(저장소 폴더 구조가 바뀌었는지 확인)")
        return {"fail": 1}
    if not APPLY:
        for title, url, text in pages[:2]:
            log(f"  --- {title}  {url}  {len(text)}자\n  {text[:300]!r}")
        return {}

    name = DATASET_PREFIX + src["name"]
    ds = find_dataset(name)
    if not ds:
        model, provider = embedding()
        st, res = dify("POST", "/datasets", {"name": name, "indexing_technique": "high_quality", "permission": "only_me",
                                             "embedding_model": model, "embedding_model_provider": provider,
                                             "description": f"{src['name']} 공식 문서(영문, 주 1회 갱신). 다른 지식과 분리"})
        if st not in (200, 201):
            sys.exit(f"[실패] 지식 베이스 만들기 {st}: {res}")
        ds = res
        log(f"  지식 베이스 만듦: {name} ({ds['id']})")
    ds_id = ds["id"]

    existing, page = {}, 1
    while True:
        st, res = dify("GET", f"/datasets/{ds_id}/documents?page={page}&limit=100")
        if st != 200:
            sys.exit(f"[실패] 문서 목록 {st}: {res}")
        existing.update({d["name"]: d["id"] for d in res.get("data", [])})
        if not res.get("has_more"):
            break
        page += 1

    path = os.path.join(STATE_DIR, f"{key}.json")
    try:
        state = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    state = state if state.get("dataset_id") == ds_id else {"dataset_id": ds_id, "docs": {}}
    docs = state.setdefault("docs", {})

    def save():
        tmp = path + ".tmp"
        json.dump(state, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, path)

    c = {"added": 0, "updated": 0, "same": 0, "removed": 0, "fail": 0}
    for n, (title, url, text) in enumerate(pages, 1):
        nm = doc_name(title, url)
        rec = docs.get(url) or {}
        doc_id = rec.get("doc_id") or existing.get(nm)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if doc_id and rec.get("sha") == digest:
            c["same"] += 1
            continue
        body = {"name": nm, "text": f"# {title}\n원문: {url}\n\n{text}", "doc_form": "hierarchical_model",
                "doc_language": "English", "indexing_technique": "high_quality", "process_rule": PROCESS_RULE}
        st = 0
        if doc_id:
            st, res = dify("POST", f"/datasets/{ds_id}/documents/{doc_id}/update-by-text", body)
            if st == 404:
                doc_id = None
        if not doc_id:
            st, res = dify("POST", f"/datasets/{ds_id}/document/create-by-text", body)
        if st != 200:
            c["fail"] += 1
            log(f"  [{n}/{len(pages)}] Dify 넣기 실패 {st}: {json.dumps(res, ensure_ascii=False)[:300]}")
            if c["added"] + c["updated"] == 0 and c["fail"] >= 3:
                sys.exit("[멈춤] 처음부터 계속 실패합니다. 위 오류(임베딩 모델·키)를 먼저 고치세요")
            continue
        c["updated" if (rec.get("doc_id") or existing.get(nm)) else "added"] += 1
        docs[url] = {"doc_id": ((res or {}).get("document") or {}).get("id") or doc_id, "sha": digest}
        save()   # 매번 저장 → 끊겨도 중복 없이 이어서
        if (c["added"] + c["updated"]) % 20 == 0:
            log(f"  [{src['name']} {n}/{len(pages)}] 추가 {c['added']} · 교체 {c['updated']} · 그대로 {c['same']} · 실패 {c['fail']}")
        time.sleep(DELAY)

    urls = {u for _, u, _ in pages}
    gone = [u for u in docs if u not in urls]
    if gone and not LIMIT:
        if len(pages) < MIN_KEEP * max(1, len(docs)):
            log(f"  [주의] 목록이 지난번 {len(docs)}개 → 이번 {len(pages)}개로 크게 줄어 삭제는 건너뜁니다")
        else:
            for u in gone:
                st, _ = dify("DELETE", f"/datasets/{ds_id}/documents/{docs[u]['doc_id']}")
                if st in (200, 204, 404):
                    c["removed"] += 1
                    docs.pop(u)
            save()
    log(f"[{src['name']}] 끝: 추가 {c['added']} · 교체 {c['updated']} · 그대로 {c['same']} · 삭제 {c['removed']} · 실패 {c['fail']}")
    return c


if __name__ == "__main__":
    want = [s.strip() for s in os.getenv("SOURCES", ",".join(SOURCES)).split(",") if s.strip()]
    bad = [s for s in want if s not in SOURCES]
    if bad:
        sys.exit(f"[실패] 모르는 문서 {bad}. 고를 수 있는 것: {', '.join(SOURCES)}")
    KEY = os.getenv("DIFY_DATASET_KEY", "").strip()
    if APPLY and not KEY and sys.stdin.isatty():
        KEY = getpass.getpass("Dify 지식 API 키: ").strip()
    if APPLY and not KEY:
        sys.exit("[실패] 지식 API 키가 없습니다(Secret dify/ncp-guide-sync). 먼저 python3 deploy/k3s/samples/ncp_guide_setup.py")
    os.makedirs(STATE_DIR, exist_ok=True)
    failed = 0
    for k in want:
        try:
            failed += (sync(k, SOURCES[k]) or {}).get("fail", 0)
        except Exception as e:   # 한 문서가 실패해도 나머지는 계속
            failed += 1
            log(f"[{SOURCES[k]['name']}] 실패: {e!r}")
    if not APPLY:
        log("\n미리보기 끝. 적재하려면 --apply")
    sys.exit(1 if failed and APPLY and failed >= len(want) else 0)
