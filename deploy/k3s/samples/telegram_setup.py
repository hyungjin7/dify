#!/usr/bin/env python3
"""뉴스 시간별 요약을 받을 텔레그램 봇 토큰·채팅방을 Secret bbb-namespace/news-telegram 에 넣는다.

kubectl 되는 곳(맥북)에서:
  1) 텔레그램에서 봇에게 아무 글이나 한 번 보낸다(그룹이면 봇을 초대한 뒤 그룹에 글 하나)
  2) cd ~/projects/dify && git pull && python3 deploy/k3s/samples/telegram_setup.py
     - 토큰은 화면에 안 보이게 묻는다(TELEGRAM_BOT_TOKEN 환경 변수로 줘도 된다)
     - 봇이 받은 최근 글에서 채팅방을 찾는다. 여러 개면 고른다(TELEGRAM_CHAT_ID 로 직접 줘도 된다)
  3) 시험 글을 보내고, 받았으면 Secret 저장
  4) 그다음 install_app.py 로 앱을 다시 올리면 워크플로 끝에서 텔레그램으로 보낸다
토큰은 저장소·화면 출력에 남기지 않는다. 표준 라이브러리만 쓴다.
"""
import getpass
import json
import os
import subprocess
import sys
import urllib.request


def tg(token, method, payload=None):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


token = (os.getenv("TELEGRAM_BOT_TOKEN") or getpass.getpass("텔레그램 봇 토큰: ")).strip()
me = tg(token, "getMe")
if not me.get("ok"):
    sys.exit("[실패] 토큰이 맞지 않습니다")
print(f"봇: @{me['result'].get('username')}")

chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
if not chat_id:
    chats = {}
    for u in tg(token, "getUpdates").get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat") or {}
        if c.get("id") is not None:
            chats[str(c["id"])] = c.get("title") or " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username") or ""
    if not chats:
        sys.exit("[실패] 봇이 받은 글이 없습니다. 텔레그램에서 봇에게 글을 하나 보내고 다시 실행하세요")
    items = list(chats.items())
    for i, (cid, name) in enumerate(items, 1):
        print(f"  {i}) {cid}  {name}")
    pick = input("받을 채팅방 번호 [1]: ").strip() or "1" if len(items) > 1 else "1"
    chat_id = items[int(pick) - 1][0]

r = tg(token, "sendMessage", {"chat_id": chat_id, "text": "뉴스 시간별 요약 연결 시험입니다."})
if not r.get("ok"):
    sys.exit(f"[실패] 시험 글을 못 보냈습니다: {r.get('description')}")
print(f"시험 글 보냄 → 채팅방 {chat_id}")

secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
          "metadata": {"name": "news-telegram", "namespace": "bbb-namespace"},
          "stringData": {"token": token, "chat_id": chat_id}}
subprocess.run(["kubectl", "apply", "-f", "-"], check=True, capture_output=True, input=json.dumps(secret).encode())
print("Secret bbb-namespace/news-telegram 저장. 이제 앱을 다시 올리세요:")
print('  SAVE_APP_KEY=news-dify-app python3 deploy/k3s/samples/install_app.py deploy/k3s/samples/news-hourly-summary-workflow.yml ""')
