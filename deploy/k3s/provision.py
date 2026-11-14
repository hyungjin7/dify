#!/usr/bin/env python3
"""Dify 준비(운영자가 kubectl 되는 곳에서 1회). 비밀번호·키는 화면·Git 에 남기지 않는다.

  python3 deploy/k3s/provision.py            # 미리보기: CNPG 주 인스턴스·백업·pgvector 확인만
  python3 deploy/k3s/provision.py --apply    # 아래를 만든다(다시 실행해도 안전, 있는 값은 그대로 둔다)

하는 일(모두 추가형):
  1) namespace dify
  2) CNPG postgre/prod-pg 에 역할 dify(LOGIN, 최소 권한), DB dify·dify_plugin(OWNER dify)
  3) dify DB 에 CREATE EXTENSION vector (pgvector — 일반 계정은 못 만들어서 여기서 만든다)
  4) Secret dify/dify-db   : DB_HOST·DB_PORT·DB_USERNAME·DB_PASSWORD·DB_DATABASE + PGVECTOR_*
  5) Secret dify/dify-secret: SECRET_KEY·INIT_PASSWORD·DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN 등 내부 키
     Secret bbb-namespace/rag-dify-key: 우리 RAG 를 Dify 외부 지식으로 부를 키(없을 때만 만들고 rag 재기동)
  6) 배포: kubectl apply -k deploy/k3s (지금 돌고 있는 우리 이미지 태그는 그대로 둔다)
다른 DB 는 건드리지 않는다. 되돌리기는 README 참고.
"""
import base64
import json
import secrets
import subprocess
import sys

NS_DB, CLUSTER, NS_APP = "postgre", "prod-pg", "dify"
HOST, PORT = "prod-pg-rw.postgre.svc.cluster.local", "5432"
ROLE, DBS = "dify", ("dify", "dify_plugin")
APPLY = "--apply" in sys.argv


def kubectl(args, input_=None):
    return subprocess.run(["kubectl"] + args, check=True, capture_output=True, input=input_).stdout.decode()


def psql(pod, db, sql):
    return kubectl(["-n", NS_DB, "exec", "-i", pod, "-c", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1",
                    "-X", "-q", "-At", "-d", db], input_=sql.encode())


def read_secret(name):
    raw = kubectl(["-n", NS_APP, "get", "secret", name, "--ignore-not-found", "-o", "json"])
    if not raw.strip():
        return {}
    return {k: base64.b64decode(v).decode() for k, v in json.loads(raw).get("data", {}).items()}


def apply_secret(name, data):
    obj = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
           "metadata": {"name": name, "namespace": NS_APP}, "stringData": data}
    kubectl(["apply", "-f", "-"], input_=json.dumps(obj).encode())


primary = kubectl(["-n", NS_DB, "get", "cluster", CLUSTER, "-o", "jsonpath={.status.currentPrimary}"]).strip()
print("Primary:", primary)
print("최근 백업 Job:", kubectl(["-n", NS_DB, "get", "jobs", "--sort-by=.metadata.creationTimestamp",
                               "-o", "custom-columns=NAME:.metadata.name,DONE:.status.succeeded,START:.status.startTime"]
                              ).strip().splitlines()[-3:])
print("pgvector 설치 가능 버전:", psql(primary, "postgres",
      "SELECT default_version FROM pg_available_extensions WHERE name='vector';").strip() or "없음")
print("max_connections:", psql(primary, "postgres", "SHOW max_connections;").strip(),
      "/ 지금 연결:", psql(primary, "postgres", "SELECT count(*) FROM pg_stat_activity;").strip())
if not APPLY:
    print("\n미리보기만 했습니다. 백업이 정상이면 --apply 로 다시 실행하세요.")
    sys.exit(0)

kubectl(["apply", "-f", "-"], input_=json.dumps(
    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS_APP}}).encode())

db = read_secret("dify-db")
password = db.get("DB_PASSWORD") or secrets.token_urlsafe(32)
pw_sql = password.replace("'", "''")
psql(primary, "postgres", f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{ROLE}') THEN
    CREATE ROLE {ROLE} LOGIN PASSWORD '{pw_sql}' NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 80;
  ELSE
    ALTER ROLE {ROLE} LOGIN PASSWORD '{pw_sql}';
  END IF;
END $$;
""" + "".join(f"""
SELECT 'CREATE DATABASE {d} OWNER {ROLE}' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='{d}')
\\gexec
REVOKE ALL ON DATABASE {d} FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE {d} TO {ROLE};
""" for d in DBS))
psql(primary, "dify", f"""
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO {ROLE};
""")
psql(primary, "dify_plugin", f"""
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO {ROLE};
""")
print("dify 확장:", psql(primary, "dify", "SELECT string_agg(extname||' '||extversion, ', ') FROM pg_extension;").strip())

apply_secret("dify-db", {
    "DB_HOST": HOST, "DB_PORT": PORT, "DB_USERNAME": ROLE, "DB_PASSWORD": password, "DB_DATABASE": "dify",
    "PGVECTOR_HOST": HOST, "PGVECTOR_PORT": PORT, "PGVECTOR_USER": ROLE, "PGVECTOR_PASSWORD": password,
    "PGVECTOR_DATABASE": "dify",
})

old = read_secret("dify-secret")
gen = {
    "SECRET_KEY": lambda: "sk-" + secrets.token_urlsafe(42),
    "INIT_PASSWORD": lambda: secrets.token_urlsafe(12)[:16],   # Dify 제한: 30자 이하
    "DIFY_AGENT_LOCAL_SANDBOX_AUTH_TOKEN": lambda: secrets.token_hex(32),
    "DIFY_AGENT_API_TOKEN": lambda: secrets.token_hex(32),
    "DIFY_AGENT_SERVER_SECRET_KEY": lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    "PLUGIN_DAEMON_KEY": lambda: secrets.token_urlsafe(42),
    "INNER_API_KEY_FOR_PLUGIN": lambda: secrets.token_urlsafe(42),
    "CODE_EXECUTION_API_KEY": lambda: secrets.token_urlsafe(32),
}
apply_secret("dify-secret", {k: old.get(k) or make() for k, make in gen.items()})
print("Secret dify-db·dify-secret 준비 완료(값은 출력하지 않음).")

# 5-0) 메일: bbb 홈페이지가 쓰는 Gmail(website-mail-secret)을 Dify 에도 쓴다. 없으면 건너뛴다(초대 링크만 표시).
try:
    raw = kubectl(["-n", "bbb-namespace", "get", "secret", "website-mail-secret", "-o", "json"])
    mail = {k: base64.b64decode(v).decode() for k, v in json.loads(raw).get("data", {}).items()}
except subprocess.CalledProcessError:
    mail = {}
if mail.get("EMAIL_HOST_USER") and mail.get("EMAIL_HOST_PASSWORD"):
    user = mail["EMAIL_HOST_USER"].strip()
    apply_secret("dify-mail", {"SMTP_USERNAME": user,
                               "SMTP_PASSWORD": mail["EMAIL_HOST_PASSWORD"].replace(" ", ""),
                               "MAIL_DEFAULT_SEND_FROM": f"Delve Dify <{user}>"})
    print("Secret dify-mail 준비 완료(Gmail, 값은 출력하지 않음).")
else:
    print("bbb-namespace/website-mail-secret 이 없어 메일 설정을 건너뜁니다(초대는 링크로).")

# 5-1) 우리 RAG(뉴스·YouTube 검색)를 Dify 외부 지식으로 쓰는 키. rag 는 bbb-namespace 에 있으므로 Secret 도 거기 둔다.
RAG_NS, RAG_SECRET = "bbb-namespace", "rag-dify-key"
raw = kubectl(["-n", RAG_NS, "get", "secret", RAG_SECRET, "--ignore-not-found", "-o", "name"]).strip()
if not raw:
    kubectl(["apply", "-f", "-"], input_=json.dumps(
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": RAG_SECRET, "namespace": RAG_NS},
         "stringData": {"key": secrets.token_urlsafe(32)}}).encode())
    kubectl(["-n", RAG_NS, "rollout", "restart", "deploy/rag"])   # 키는 기동 때 읽는다
    print("Secret bbb-namespace/rag-dify-key 를 만들고 rag 를 다시 띄웠습니다.")

# 6) 배포. 손으로 돌린 CI 가 우리 이미지(gitea 레지스트리)로 바꿔 둔 배포는 그 태그를 유지한다.
import os
import tempfile
here = os.path.dirname(os.path.abspath(__file__))
REG = "gitea.delvrio.com/hyungjin7/dify"
images = []
for official, ours, deploy in (("langgenius/dify-api", "api", "dify-api"),
                               ("langgenius/dify-web", "web", "dify-web"),
                               ("langgenius/dify-agent-backend", "agent", "dify-agent-backend"),
                               ("langgenius/dify-agent-local-sandbox", "local-sandbox", "dify-local-sandbox")):
    try:
        cur = kubectl(["-n", NS_APP, "get", "deploy", deploy, "-o",
                       "jsonpath={.spec.template.spec.containers[0].image}"]).strip()
    except subprocess.CalledProcessError:
        cur = ""
    if cur.startswith(f"{REG}/{ours}:"):
        images.append((official, cur.rsplit(":", 1)))
# 임시 kustomization 은 옆 폴더(deploy/.apply-*)에 만든다. /tmp 는 macOS 링크(/var → /private/var)로,
# k3s 안쪽은 kustomize 순환 오류로 실패한다(2026-11-14).
with tempfile.TemporaryDirectory(dir=os.path.dirname(here), prefix=".apply-") as tmp:
    with open(os.path.join(tmp, "kustomization.yaml"), "w") as f:
        f.write(f"resources:\n- ../{os.path.basename(here)}\n")
        if images:
            f.write("images:\n")
            for official, (name, tag) in images:
                f.write(f"- name: {official}\n  newName: {name}\n  newTag: \"{tag}\"\n")
                print(f"유지: {official} → {name}:{tag}")
    r = subprocess.run(["kubectl", "apply", "-k", tmp], capture_output=True, text=True)
    print(r.stdout.strip())
    if r.returncode:
        print(r.stderr.strip(), file=sys.stderr)
        sys.exit(r.returncode)
print("RAG 외부 지식 키 보기: kubectl -n bbb-namespace get secret rag-dify-key -o jsonpath='{.data.key}' | base64 -d; echo")
print("첫 관리자 비밀번호 보기: kubectl -n dify get secret dify-secret -o jsonpath='{.data.INIT_PASSWORD}' | base64 -d; echo")
