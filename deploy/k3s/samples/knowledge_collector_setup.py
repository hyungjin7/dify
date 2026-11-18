#!/usr/bin/env python3
"""지식 출처 모음 수집기 키 준비(한 번만, kubectl 되는 곳에서). 2026-11-19.

수집기(dify ns, Deployment knowledge-collector)와 bbb agent 가 같은 키를 나눠 갖는다:
  - Secret dify/knowledge-collector            {token}  ← 수집기가 agent 를 부를 때
  - Secret bbb-namespace/knowledge-collector-token {token}  ← agent 가 받을 때 확인
이미 있으면 그 값을 그대로 쓴다(다시 돌려도 안전). 만든 뒤 두 Deployment 를 다시 띄운다.

  python3 deploy/k3s/samples/knowledge_collector_setup.py
"""
import base64
import json
import secrets
import subprocess
import sys


def kubectl(*args, input_=None):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, input=input_)


def read(ns, name, key):
    r = kubectl("-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(r.stdout).decode() if r.returncode == 0 and r.stdout else ""


def write(ns, name, key, value):
    body = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": name, "namespace": ns},
            "stringData": {key: value}}
    r = kubectl("apply", "-f", "-", input_=json.dumps(body))
    if r.returncode:
        sys.exit(f"[실패] Secret {ns}/{name} 저장: {r.stderr.strip()}")
    print(f"  Secret {ns}/{name} 저장")


token = read("dify", "knowledge-collector", "token") or read("bbb-namespace", "knowledge-collector-token", "token") \
    or secrets.token_urlsafe(32)
print("[1] 수집기 키 준비")
write("dify", "knowledge-collector", "token", token)
write("bbb-namespace", "knowledge-collector-token", "token", token)
print("[2] 다시 띄우기")
for ns, dep in (("bbb-namespace", "agent"), ("dify", "knowledge-collector")):
    r = kubectl("-n", ns, "rollout", "restart", f"deploy/{dep}")
    print(f"  {ns}/{dep}: {'다시 띄움' if r.returncode == 0 else r.stderr.strip()}")
print("""
끝. 진행 보기: kubectl -n dify logs -f deploy/knowledge-collector
예전 주간 작업은 지운다: kubectl -n dify delete cronjob tech-docs-sync""")
