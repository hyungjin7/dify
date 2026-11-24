#!/usr/bin/env python3
"""텔레그램 봇 토큰·받을 방(채팅방·그룹·채널)을 bbb-namespace Secret 에 넣는다.

방마다 Secret 이 따로다(봇은 하나를 같이 써도 된다):
  - news-telegram    : 버핏 뉴스 요약 (기본값)
  - startup-telegram : 신규 창업 보고서(AI Native 생산성 제품 창조) 전용 채널

kubectl 되는 곳(맥북)에서:
  1) 텔레그램에서 받을 곳을 준비한다
     - 채팅방: 봇에게 아무 글이나 한 번 보낸다
     - 그룹: 봇을 초대한 뒤 그룹에 글 하나
     - 채널: 봇을 채널 관리자로 넣고 '메시지 게시' 권한을 켠 뒤 채널에 글 하나
  2) cd ~/projects/dify && git pull
     python3 deploy/k3s/samples/telegram_setup.py                                   # 버핏 뉴스 방
     TELEGRAM_SECRET=startup-telegram python3 deploy/k3s/samples/telegram_setup.py  # 신규 창업 보고서 채널
     - 토큰: 새 Secret 이면 news-telegram 의 토큰을 다시 쓸지 묻는다. 아니면 화면에 안 보이게 묻는다
       (TELEGRAM_BOT_TOKEN 환경 변수로 줘도 된다)
     - 이 컴퓨터에서 텔레그램이 막히면 자동으로 클러스터 Pod(deploy/dworker)에서 대신 부른다(TELEGRAM_VIA_CLUSTER=1 로 처음부터)
     - 받을 방: 봇이 받은 최근 글에서 찾는다. 여러 개면 고른다(TELEGRAM_CHAT_ID 로 직접 줘도 된다. 채널은 -100 으로 시작)
  3) 시험 글을 보내고, 받았으면 Secret 저장
  4) 그다음 install_app.py 로 앱을 다시 올리면 워크플로 끝에서 그 방으로 보낸다
토큰은 저장소·화면 출력에 남기지 않는다. 표준 라이브러리만 쓴다.
"""
import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

SECRET = (os.getenv("TELEGRAM_SECRET") or "news-telegram").strip()
LABEL = {"news-telegram": "버핏 뉴스 요약", "startup-telegram": "신규 창업 보고서"}.get(SECRET, SECRET)
REUSE_FROM = "news-telegram"


# 이 컴퓨터에서 api.telegram.org 가 막히거나 느리면(2026-11-24 실측: 읽기 시간 초과) 클러스터 안 Pod 에서 대신 부른다.
# 클러스터는 Dify·news 가 이미 텔레그램으로 보내고 있어 닿는다. 토큰은 명령줄이 아니라 표준 입력으로 넘긴다.
RELAY = (os.getenv("TELEGRAM_RELAY_DEPLOY") or "deploy/dworker").strip()
_RELAY_CODE = ("import json,sys,urllib.request,urllib.error\n"
               "a=json.load(sys.stdin)\n"
               "r=urllib.request.Request(a['url'],data=a['data'].encode() if a['data'] else None,"
               "headers={'Content-Type':'application/json'})\n"
               "try:\n  print(urllib.request.urlopen(r,timeout=30).read().decode())\n"
               "except urllib.error.HTTPError as e:\n  print(e.read().decode() or json.dumps({'ok':False,'description':'HTTP %s'%e.code}))\n")
_use_relay = os.getenv("TELEGRAM_VIA_CLUSTER", "").strip() == "1"


def _relay(url, data):
    out = subprocess.run(["kubectl", "-n", "bbb-namespace", "exec", "-i", RELAY, "--", "python", "-c", _RELAY_CODE],
                         input=json.dumps({"url": url, "data": data}), capture_output=True, text=True, timeout=90)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "description": f"클러스터 경유 실패: {(out.stderr or out.stdout)[-300:]}"}


def tg(token, method, payload=None):
    global _use_relay
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload) if payload is not None else ""
    if not _use_relay:
        req = urllib.request.Request(url, data=data.encode() if data else None, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:  # 텔레그램은 실패도 JSON 으로 알려 준다
            try:
                return json.load(e)
            except Exception:  # noqa: BLE001
                return {"ok": False, "description": f"HTTP {e.code}"}
        except (OSError, TimeoutError) as e:   # 연결 안 됨·시간 초과 → 이후는 클러스터로
            print(f"    → 이 컴퓨터에서 텔레그램 연결 실패({type(e).__name__}), 클러스터({RELAY})에서 대신 부릅니다")
            _use_relay = True
    return _relay(url, data)


def secret_value(name, field):
    try:
        out = subprocess.run(["kubectl", "-n", "bbb-namespace", "get", "secret", name,
                              "-o", "jsonpath={.data." + field + "}"], check=True, capture_output=True, text=True).stdout
        return base64.b64decode(out).decode().strip() if out else ""
    except Exception:  # noqa: BLE001
        return ""


print(f"받을 곳: {LABEL} → Secret bbb-namespace/{SECRET}")
token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
if not token and SECRET != REUSE_FROM:
    old = secret_value(REUSE_FROM, "token")
    if old and (input(f"{REUSE_FROM} 의 봇을 그대로 쓸까요? [Y/n]: ").strip().lower() or "y") == "y":
        token = old
if not token:
    token = getpass.getpass("텔레그램 봇 토큰: ").strip()
me = tg(token, "getMe")
if not me.get("ok"):
    sys.exit("[실패] 토큰이 맞지 않습니다")
print(f"봇: @{me['result'].get('username')}")

chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
if not chat_id:
    up = tg(token, "getUpdates", {"allowed_updates": ["message", "channel_post", "my_chat_member"]})
    if not up.get("ok"):
        sys.exit(f"[실패] 봇이 받은 글을 못 읽었습니다: {up.get('description')}\n"
                 "  웹훅이 걸린 봇이면 TELEGRAM_CHAT_ID=<방 id> 로 직접 주세요")
    chats = {}
    for u in up.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat") or {}
        if c.get("id") is not None:
            name = c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username") or ""
            chats[str(c["id"])] = f"[{c.get('type', '')}] {name}"
    if not chats:
        sys.exit("[실패] 봇이 받은 글이 없습니다. 채널이면 봇을 관리자로 넣고 채널에 글을 하나 올린 뒤 다시 실행하세요")
    items = list(chats.items())
    for i, (cid, name) in enumerate(items, 1):
        print(f"  {i}) {cid}  {name}")
    pick = (input("받을 방 번호 [1]: ").strip() or "1") if len(items) > 1 else "1"
    chat_id = items[int(pick) - 1][0]

r = tg(token, "sendMessage", {"chat_id": chat_id, "text": f"{LABEL} 연결 시험입니다."})
if not r.get("ok"):
    hint = " (채널이면 봇에게 '메시지 게시' 관리자 권한이 있어야 합니다)" if chat_id.startswith("-100") else ""
    sys.exit(f"[실패] 시험 글을 못 보냈습니다: {r.get('description')}{hint}")
print(f"시험 글 보냄 → {chat_id}")

secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
          "metadata": {"name": SECRET, "namespace": "bbb-namespace"},
          "stringData": {"token": token, "chat_id": chat_id}}
subprocess.run(["kubectl", "apply", "-f", "-"], check=True, capture_output=True, input=json.dumps(secret).encode())
print(f"Secret bbb-namespace/{SECRET} 저장. 이제 그 앱을 install_app.py 로 다시 올리세요")
