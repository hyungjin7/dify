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
sleep 5   # 새 파드는 NetworkPolicy 허용 목록에 들어가기까지 몇 초 걸린다(바로 부르면 000)
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
echo "== 4-2) 최근 30분 응답 상태 개수(오류·웹소켓) =="
kubectl -n $NS logs deploy/dify-nginx --since=30m 2>/dev/null | grep -E '^[0-9.]+ ' \
  | awk '{k=$3; if ($5 ~ /socket.io/) k=k" socket.io"; else if ($3 >= 400) k=k" "$5; c[k]++} END {for (k in c) print c[k], k}' | sort -rn | head -15

echo
echo "== 6) 브라우저가 받는 화면 파일(JS·CSS): 크기 · 압축 · 캐시 · 시간 =="
html=$(curl -s -L "$URL/signin")
assets=$(printf '%s' "$html" | grep -oE '/_next/static/[^"'"'"' ]+\.(js|css)' | sort -u)
n=0; total=0
for a in $assets; do
  n=$((n+1))
  line=$(curl -s -o /dev/null -D - -H 'Accept-Encoding: br, gzip' -w 'TIME=%{time_total} SIZE=%{size_download}' "$URL$a")
  sz=$(printf '%s' "$line" | grep -oE 'SIZE=[0-9]+' | cut -d= -f2); total=$((total+sz))
  if [ $n -le 8 ]; then
    enc=$(printf '%s' "$line" | grep -i '^content-encoding' | tr -d '\r' | cut -d' ' -f2)
    cc=$(printf '%s' "$line" | grep -i '^cache-control' | tr -d '\r' | cut -d' ' -f2-)
    t=$(printf '%s' "$line" | grep -oE 'TIME=[0-9.]+' | cut -d= -f2)
    printf '%-70s %8sB 압축=%-5s 시간=%ss 캐시=%s\n' "${a:0:70}" "$sz" "${enc:-없음}" "$t" "$cc"
  fi
done
echo "로그인 화면 파일 ${n}개, 내려받은 합계 ${total}B"
printf '%s' "$html" | grep -oE 'https?://[a-zA-Z0-9.-]+' | grep -v 'dify.delvrio.com' | sort | uniq -c | sort -rn | head -10 | sed 's/^/바깥 주소 참조: /'

echo
echo "== 5) 파드 자원·재시작 =="
kubectl -n $NS top pods 2>/dev/null
kubectl -n $NS get pods -o wide

echo
echo "== 7) api 가 다른 일에 막히는지: ping 20번(채팅 답이 나오는 중에 돌리면 더 정확) =="
for i in $(seq 20); do curl -s -o /dev/null -w '%{time_total}\n' "$URL/console/api/ping"; done \
  | sort -n | awk '{a[NR]=$1} END {printf "가장 빠름 %ss · 가운데 %ss · 가장 느림 %ss\n", a[1], a[int(NR/2)], a[NR]}'
