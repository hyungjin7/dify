#!/usr/bin/env bash
# Dify 화면이 느릴 때 "어느 구간이 느린지" 재는 스크립트. 맥북(kubectl 되는 곳)에서 실행한다. 읽기만 한다.
#   bash deploy/k3s/tools/ui-speed-check.sh
# 1) 바깥(맥북 → dify.delvrio.com)  2) 클러스터 안(nginx·web·api 직접)  3) api → Redis·DB  4) 최근 느린 요청 상위
set -u
NS=dify
URL=https://dify.delvrio.com
FMT='%{http_code}  dns=%{time_namelookup}s  연결=%{time_connect}s  tls=%{time_appconnect}s  첫바이트=%{time_starttransfer}s  전체=%{time_total}s  크기=%{size_download}B'

echo "== 1) 맥북 → $URL (Traefik 거쳐서) =="
for p in / /apps /console/api/system-features /console/api/ping; do
  printf '%-32s ' "$p"; curl -s -o /dev/null --compressed -w "$FMT\n" "$URL$p"
done

echo
echo "== 2) 클러스터 안에서 직접 (Traefik 빼고) =="
kubectl -n $NS run ui-speed-check --rm -i --quiet --restart=Never --image=curlimages/curl:8.11.1 -- sh -c "
for u in http://dify-nginx/apps http://dify-web:3000/apps http://dify-api:5001/console/api/system-features http://dify-api:5001/console/api/ping; do
  printf '%-52s ' \$u; curl -s -o /dev/null --compressed -w '$FMT\n' \$u
done"

echo
echo "== 3) api 파드 → Redis · DB · DNS =="
kubectl -n $NS exec -i deploy/dify-api -c api -- python - <<'EOF'
import os, socket, time
import redis, psycopg2
def ms(t): return f"{(time.time()-t)*1000:.1f}ms"
for h in [os.environ["REDIS_HOST"], os.environ["DB_HOST"], "dify-plugin-daemon"]:
    t = time.time(); socket.getaddrinfo(h, 80); print(f"DNS {h}: {ms(t)}")
r = redis.Redis(host=os.environ["REDIS_HOST"], port=int(os.environ["REDIS_PORT"]), db=int(os.environ["REDIS_DB"]))
r.ping(); t = time.time()
for _ in range(100): r.ping()
print(f"Redis ping 평균: {(time.time()-t)*10:.2f}ms")
kw = dict(host=os.environ["DB_HOST"], port=os.environ["DB_PORT"], user=os.environ["DB_USERNAME"],
          password=os.environ["DB_PASSWORD"], dbname=os.environ["DB_DATABASE"])
t = time.time(); c = psycopg2.connect(**kw); print(f"DB 새 연결: {ms(t)}")
cur = c.cursor(); t = time.time()
for _ in range(100): cur.execute("select 1"); cur.fetchone()
print(f"DB select 1 평균: {(time.time()-t)*10:.2f}ms")
cur.execute("select count(*) from pg_stat_activity where datname='dify'"); print("dify DB 연결 수:", cur.fetchone()[0])
EOF

echo
echo "== 4) 최근 30분 느린 요청 상위 20 (초: 전체 · 뒤쪽 · 상태 · 경로) =="
kubectl -n $NS logs deploy/dify-nginx --since=30m 2>/dev/null \
  | grep -E '^[0-9.]+ ' | grep -v -E '/socket.io/|chat-messages|workflows/run|/stream' \
  | sort -rn | head -20

echo
echo "== 5) 파드 자원·재시작 =="
kubectl -n $NS top pods 2>/dev/null
kubectl -n $NS get pods -o wide
