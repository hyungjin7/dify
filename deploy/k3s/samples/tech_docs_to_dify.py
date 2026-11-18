#!/usr/bin/env python3
"""인프라 기술 문서(Kubernetes·PostgreSQL·Traefik·Argo CD·Helm)를 Dify 지식 베이스에 넣고 바뀐 것만 갱신한다.

NCP 가이드(ncp_guide_to_dify.py)와 같은 방식이고(2026-11-18 사용자 요청), 문서마다 지식 베이스를 따로 둔다:
  "기술문서 · Kubernetes" / "· PostgreSQL" / "· Traefik" / "· Argo CD" / "· Helm"
  → 한 문서만 골라 묻거나(정확), 다섯 개를 함께 물을 수 있다. 다른 지식(rag·NCP)과 섞이지 않는다.

가져오는 방법(kind, 2026-11-19 지식 출처 모음 — 출처는 ccc › 지식 출처 모음 화면에서 더한다):
  - git:   문서 원문(md·mdx·rst)이 GitHub 저장소에 있다 → 저장소 묶음(tar.gz) 1번 받기(ref 여러 개면 차례로 시도)
  - html:  같은 폴더의 *.html 을 따라간다(PostgreSQL, 릴리스 노트 제외)
  - crawl: 시작 주소 아래(prefix)의 링크를 따라가며 본문(main·article·알려진 본문 칸)만 뽑는다. max 쪽 상한, 1초에 1쪽
  - links: 목록 한 쪽에서 정해진 모양(pattern)의 링크만 받는다(MIT 6.5840 강의 노트 .txt)
  - wiki:  MediaWiki 분류(categories)에 든 문서를 원문 위키텍스트(action=raw)로 받는다(ArchWiki)
  - llms:  사이트의 /llms.txt 목록 → 각 쪽 .md(없으면 HTML 본문)
GCP·AWS·Azure 문서는 긁지 않는다: 각 회사가 공식 MCP 검색을 무료로 열어 두어 bbb 골라 묻기가 질문 때 바로 부른다
(사용자 결정 2026-11-19 "이미 제공하면 가져다 쓴다", bbb ncp_guide.EXTERNAL).
저작권: 사내 참고용으로만 쓰고, 답에는 원문 링크를 붙인다. roadmap.sh 는 "개인 사용만" 라이선스라 넣지 않는다(사용자 결정 대기 2026-11-19).

갱신 규칙(상태 STATE_DIR/<문서>.json = 주소 → 문서 id·원문 지문): 새 주소 → 추가, 원문이 바뀜 → 그 문서만 교체,
목록에서 사라짐 → 삭제(목록이 지난번의 MIN_KEEP_RATIO 밑으로 줄면 사이트 문제로 보고 지우지 않는다).
진행 기록은 문서 하나 넣을 때마다 저장한다(중간에 끊어도 다시 돌리면 이어서, 중복 없음).

  보통은 상주 수집기(Deployment knowledge-collector)가 돌린다. 손으로 돌릴 때:
  SOURCES_FILE=src.json python3 deploy/k3s/samples/tech_docs_to_dify.py           # 미리보기(문서별 페이지 수·샘플)
  SOURCES_FILE=src.json python3 deploy/k3s/samples/tech_docs_to_dify.py --apply   # 적재/갱신
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

# 문서 목록의 정본은 bbb agent 의 지식 출처 모음 표(knowledge_sources, 2026-11-19 사용자 결정 — 코드 목록은 없앤다).
# 상주 수집기(knowledge_collector.py)가 그 표에서 할 일을 받아 아래 sync() 를 부른다.
# 손으로 돌릴 때는 출처 설정을 JSON 파일로 준다: SOURCES_FILE=src.json (목록 [{"id","name","kind",…설정}] — 화면 "수집 설정"과 같은 모양)
def load_sources():
    path = os.getenv("SOURCES_FILE", "").strip()
    if not path:
        return {}
    rows = json.load(open(path, encoding="utf-8"))
    return {r["id"]: r for r in (rows if isinstance(rows, list) else [rows])}


SOURCES = load_sources()
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


def clean_rst(text):
    """reStructuredText(Sphinx) → 읽을 만한 글자. 제목 = 밑줄 친 첫 줄."""
    title = ""
    m = re.search(r"^(\S[^\n]*)\n([=\-~^\"'`#*+]{3,})\s*$", text, re.M)
    if m:
        title = m.group(1).strip()
    text = re.sub(r"^\.\. (only|include|toctree|raw|image|figure|tabs|spelling)::.*?(?=^\S|\Z)", "", text, flags=re.M | re.S)
    text = re.sub(r"^\.\. _[^:]+:\s*$", "", text, flags=re.M)              # 이름표
    text = re.sub(r":(ref|doc|term|option|command|ghissue):`([^`<]*?)(?:\s*<[^>]*>)?`", r"\2", text)
    text = re.sub(r"^(\S[^\n]*)\n[=\-~^\"'`#*+]{3,}\s*$", r"## \1", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def git_pages(src):
    """저장소 묶음을 흘려 읽는다(통째로 메모리에 올리지 않음 — kubernetes/website 는 수백 MB)."""
    refs = src["ref"] if isinstance(src["ref"], list) else [src["ref"]]
    exclude = re.compile(src["exclude"]) if src.get("exclude") else None
    suffix = src.get("suffix", "/")
    found = {d: [] for d in src["dirs"]}
    tries = [(ref, i) for ref in refs for i in range(3 if ref == refs[-1] else 1)]
    for ref, i in tries:
        url = f"https://codeload.github.com/{src['repo']}/tar.gz/refs/heads/{ref}"
        log(f"  저장소 묶음 받는 중: {src['repo']}@{ref}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, \
                    tarfile.open(fileobj=r, mode="r|gz") as tar:
                for m in tar:
                    if not m.isfile() or not m.name.endswith((".md", ".mdx", ".rst")):
                        continue
                    inner = m.name.split("/", 1)[-1]
                    d = next((d for d in src["dirs"] if inner.startswith(d + "/")), None)
                    if not d:
                        continue
                    rel = inner[len(d) + 1:]
                    if exclude and exclude.search(rel):
                        continue
                    raw = tar.extractfile(m).read().decode("utf-8", "replace")
                    title, text = clean_rst(raw) if m.name.endswith(".rst") else clean_md(raw)
                    if len(text) < 80:
                        continue
                    path = re.sub(r"\.(mdx?|rst)$", "", rel)
                    path = re.sub(r"(^|/)(_?index|README)$", r"\1", path, flags=re.I)
                    page_url = src["base"] + (path + suffix if path and not path.endswith("/") else path)
                    found[d].append((title or posixpath.basename(path.rstrip("/")) or src["name"], page_url, text))
            break
        except urllib.error.HTTPError as e:
            if e.code == 404 and ref != refs[-1]:   # 그 이름의 브랜치가 없다 → 다음 ref
                continue
            if (ref, i) == tries[-1]:
                raise RuntimeError(f"{url} 받기 실패: {e!r}")
            time.sleep(10 * (i + 1))
        except (urllib.error.URLError, TimeoutError, tarfile.TarError, OSError) as e:
            if (ref, i) == tries[-1]:
                raise RuntimeError(f"{url} 받기 실패: {e!r}")
            time.sleep(10 * (i + 1))
    return next((found[d] for d in src["dirs"] if found[d]), [])   # 먼저 적힌 폴더 우선


# ---------- PostgreSQL HTML → 글자 ----------
class DocText(HTMLParser):
    BLOCK = {"p", "div", "li", "tr", "dt", "dd", "table", "br", "ul", "ol", "dl", "blockquote"}
    SKIP_CLASS = ("navheader", "navfooter", "toc")
    SKIP_TAGS = {"nav", "aside", "footer", "script", "style", "header"}

    def __init__(self, match=None):
        super().__init__(convert_charrefs=True)
        # 본문 칸을 알아보는 규칙(기본 = PostgreSQL docContent). crawl 은 main·article·알려진 본문 칸.
        self.match = match or (lambda tag, a: tag == "div" and a.get("id") == "docContent")
        self.ctag = "div"
        self.out, self.title, self.links = [], "", []
        self.depth = 0          # 본문 칸 안 깊이(0 = 밖)
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
        if not self.depth and self.match(tag, a):
            self.depth, self.ctag = 1, tag
            return
        if not self.depth:
            return
        if tag == self.ctag:
            self.depth += 1
        if self.skip:
            if tag in self.SKIP_TAGS or tag == "div":
                self.skip += 1
            return
        if tag in self.SKIP_TAGS or (tag == "div" and any(c in (a.get("class") or "") for c in self.SKIP_CLASS)):
            self.skip = 1
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
        if tag == self.ctag:
            self.depth -= 1
            if not self.depth:
                return
        if self.skip:
            if tag in self.SKIP_TAGS or tag == "div":
                self.skip -= 1
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


# ---------- crawl · links · wiki · llms (2026-11-19) ----------
MAIN_IDS = ("main-col-body", "main-content", "content", "docs-content", "bodyContent", "mw-content-text")
MAIN_CLASS = ("devsite-article-body", "markdown-body", "rst-content", "article-body", "md-content")


def main_body(tag, a):
    cls = a.get("class") or ""
    return tag in ("main", "article") or a.get("id") in MAIN_IDS or any(c in cls for c in MAIN_CLASS)


def page_text(body):
    """HTML 한 쪽 → (제목, 본문 글자, 링크들). 본문 칸을 못 찾으면 태그만 걷어낸다."""
    p = DocText(match=main_body)
    p.feed(body)
    text = p.text()
    if len(text) < 80:
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>|<[^>]+>", " ",
                                                           body, flags=re.S | re.I))).strip()
    title = re.split(r"\s+[|\-–—]\s+", html.unescape(p.head).strip())[0].strip()
    return title, text, p.links


def _norm(url):
    u = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", ""))


def crawl_pages(src):
    """시작 주소들에서 prefix 아래 링크만 따라간다(너비 우선, max 쪽, 1쪽마다 PAGE_DELAY)."""
    starts = src["start"] if isinstance(src["start"], list) else [src["start"]]
    prefixes = src.get("prefix") or [s0 if s0.endswith("/") else s0.rsplit("/", 1)[0] + "/" for s0 in starts]
    prefixes = prefixes if isinstance(prefixes, list) else [prefixes]
    exclude = re.compile(src["exclude"]) if src.get("exclude") else None
    cap = LIMIT or src.get("max", 500)
    queue, seen, pages = [_norm(x) for x in starts], {_norm(x) for x in starts}, []
    while queue and len(pages) < cap:
        url = queue.pop(0)
        st, body = get(url)
        time.sleep(PAGE_DELAY)
        if st != 200 or "<html" not in body[:2000].lower():
            continue
        title, text, links = page_text(body)
        for href in links:
            u = _norm(urllib.parse.urljoin(url, href))
            if u in seen or not any(u.startswith(px) for px in prefixes) or (exclude and exclude.search(href)):
                continue
            if re.search(r"\.(png|jpe?g|gif|svg|pdf|zip|gz|mp4)$", u, re.I):
                continue
            seen.add(u)
            queue.append(u)
        if len(text) >= 200:
            pages.append((title or url.rstrip("/").rsplit("/", 1)[-1], url, text))
        if len(pages) % 100 == 0 and pages:
            log(f"  {src['name']} 페이지 {len(pages)}개 받음…")
    return pages


def link_pages(src):
    """목록 한 쪽에서 pattern 에 맞는 링크만(글자 파일·HTML)."""
    st, body = get(src["index"])
    if st != 200:
        raise RuntimeError(f"{src['index']} 받기 실패 {st}")
    pat, pages, seen = re.compile(src["pattern"]), [], set()
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', body, flags=re.S | re.I):
        u = _norm(urllib.parse.urljoin(src["index"], href))
        if u in seen or not pat.search(u):
            continue
        seen.add(u)
        st, raw = get(u)
        time.sleep(PAGE_DELAY)
        if st != 200:
            continue
        title = re.sub(r"<[^>]+>|\s+", " ", html.unescape(label)).strip() or u.rsplit("/", 1)[-1]
        text = page_text(raw)[1] if "<html" in raw[:2000].lower() else raw.strip()
        if len(text) >= 200:
            pages.append((title, u, text))
    return pages


def clean_wikitext(text):
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:Category|[a-z]{2}(?:-[a-z]+)?):[^\]]*\]\]", "", text)
    text = re.sub(r"\[\[[^|\]]*\|([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"^(=+)\s*(.*?)\s*\1\s*$", lambda m: "#" * max(2, len(m.group(1))) + " " + m.group(2), text, flags=re.M)
    text = re.sub(r"'{2,}", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def wiki_pages(src):
    """MediaWiki: 분류마다 문서 이름(API categorymembers) → 원문(index.php?action=raw). 영문 문서만."""
    api, base = src["base"] + "api.php", src["base"]
    titles = []
    for cat in src["categories"]:
        cont = ""
        while True:
            q = {"action": "query", "list": "categorymembers", "cmtitle": f"Category:{cat}", "cmlimit": "500",
                 "cmnamespace": "0", "format": "json"}
            if cont:
                q["cmcontinue"] = cont
            st, body = get(api + "?" + urllib.parse.urlencode(q))
            time.sleep(PAGE_DELAY)
            if st != 200:
                break
            d = json.loads(body or "{}")
            titles += [m["title"] for m in d.get("query", {}).get("categorymembers", [])
                       if not re.search(r"\((?:\w+ )?(?:简体中文|日本語|Русский|Español|Deutsch|Français|Português|Italiano|"
                                        r"Polski|Українська|한국어|Magyar|Čeština|Suomi|Ελληνικά|العربية|Türkçe|"
                                        r"Nederlands|Svenska|Dansk|Bahasa Indonesia|Tiếng Việt|繁體中文|正體中文)\)$", m["title"])]
            cont = d.get("continue", {}).get("cmcontinue", "")
            if not cont:
                break
    pages, cap = [], LIMIT or src.get("max", 1000)
    for t in dict.fromkeys(titles):
        if len(pages) >= cap:
            break
        page = base + "title/" + urllib.parse.quote(t.replace(" ", "_"))
        st, raw = get(base + "index.php?" + urllib.parse.urlencode({"title": t, "action": "raw"}))
        time.sleep(PAGE_DELAY)
        if st != 200 or raw.lstrip().upper().startswith("#REDIRECT"):
            continue
        text = clean_wikitext(raw)
        if len(text) >= 200:
            pages.append((t, page, text))
    return pages


def llms_pages(src):
    """사이트 /llms.txt 목록 → 각 쪽 .md(이미 .md 면 그대로, 마크다운이 아니면 HTML 본문)."""
    site = src["site"].rstrip("/")
    st, idx = get(site + "/llms.txt")
    if st != 200:
        raise RuntimeError(f"{site}/llms.txt 받기 실패 {st}")
    host = urllib.parse.urlsplit(site).netloc
    pages, seen, cap = [], set(), LIMIT or src.get("max", 2000)
    for title, url in re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", idx):
        url = url.split("#")[0]
        if url in seen or urllib.parse.urlsplit(url).netloc != host:
            continue
        seen.add(url)
        if len(pages) >= cap:
            break
        md = url if url.endswith(".md") else url.rstrip("/") + ".md"
        st, text = get(md)
        if st != 200 or text.lstrip().startswith("<"):
            st, body = get(url)
            text = page_text(body)[1] if st == 200 else ""
        time.sleep(PAGE_DELAY)
        if len(text.strip()) >= 80:
            pages.append((title.strip(), url.removesuffix(".md"), clean_md(text)[1]))
    return pages


KINDS = {"git": lambda s0: git_pages(s0), "html": lambda s0: html_pages(s0), "crawl": lambda s0: crawl_pages(s0),
         "links": lambda s0: link_pages(s0), "wiki": lambda s0: wiki_pages(s0), "llms": lambda s0: llms_pages(s0)}


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
    pages = KINDS[src["kind"]](src)
    pages = list({u: (t, u, x) for t, u, x in pages}.values())
    if LIMIT:
        pages = pages[:LIMIT]
    log(f"[{src['name']}] 넣을 페이지 {len(pages)}개")
    if not pages:
        log(f"[{src['name']}] 페이지가 없어 건너뜁니다(저장소 폴더 구조가 바뀌었는지 확인)")
        return {"fail": 1, "pages": 0, "message": "페이지가 없음(주소·폴더 구조 확인)"}
    if not APPLY:
        for title, url, text in pages[:2]:
            log(f"  --- {title}  {url}  {len(text)}자\n  {text[:300]!r}")
        return {}

    name = src.get("dataset") or DATASET_PREFIX + src["name"]
    ds = find_dataset(name)
    if not ds:
        model, provider = embedding()
        st, res = dify("POST", "/datasets", {"name": name, "indexing_technique": "high_quality", "permission": "only_me",
                                             "embedding_model": model, "embedding_model_provider": provider,
                                             "description": f"{src['name']} 문서({'한국어' if src.get('lang') == 'Korean' else '영문'}, 주 1회 갱신). 다른 지식과 분리"})
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
                "doc_language": src.get("lang", "English"), "indexing_technique": "high_quality", "process_rule": PROCESS_RULE}
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
    c.update(dataset_id=ds_id, pages=len(docs))
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
