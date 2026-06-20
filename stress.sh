#!/usr/bin/env bash
# stress.sh — stress-test the LinkedIn scraper API with curl, measure request
# → response latency, and emit a Markdown report.
#
# The scraper serves every request through ONE serialized browser-worker thread,
# so concurrency here exercises queueing / backpressure, not parallelism.
#
# Usage:
#   ./stress.sh                       (uses the baked-in secret)
#   API_KEY=... BASE_URL=http://127.0.0.1:5000 CONCURRENCY=4 REPEATS=3 ./stress.sh
#
# Env knobs (all optional — API_KEY defaults to the shared secret from
# api-examples.md, overridable if you point at a differently-keyed server):
#   API_KEY       shared secret whose SHA-256 is in the server's .env
#   BASE_URL      server base URL            (default: deployed server)
#   CONCURRENCY   parallel in-flight workers  (default: 4)
#   REPEATS       hits per target             (default: 3)
#   TIMEOUT       per-request curl timeout s  (default: 120)
#   OUT           report path                 (default: stress-results-<ts>.md)
set -uo pipefail

# ---------- config ----------
# No secrets baked in. Pass both at runtime:
#   BASE_URL=http://host:port API_KEY='your-key' ./stress.sh
BASE_URL="${BASE_URL:-http://127.0.0.1:5000}"
API_KEY="${API_KEY:?must be set — pass API_KEY='your-key' ./stress.sh}"
CONCURRENCY="${CONCURRENCY:-4}"
REPEATS="${REPEATS:-3}"
TIMEOUT="${TIMEOUT:-150}"
TS="$(date +%Y%m%d-%H%M%S)"
OUT="${OUT:-stress-results-$TS.md}"

command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }


# ---------- targets ----------
# Profiles (real LinkedIn /in/ URLs to scrape). Two endpoints each so we cover
# both the typed (/profile) and raw-DOM (/extract) code paths per person.
P1='https://www.linkedin.com/in/simone-rizzetto/'
P2='https://www.linkedin.com/in/lisa-ezhergina/'
P3='https://www.linkedin.com/in/eugenevkim/'
P4='https://www.linkedin.com/in/francescodeliva/?locale=en'
# Companies (typed + full multi-tab /extract).
C1='https://www.linkedin.com/company/kruncher/'
C2='https://www.linkedin.com/company/freda-ab/'

LABELS=(); ENDPOINTS=(); BODIES=(); TIMEOUTS=()
# add <label> <endpoint> <body> [timeout_override]
add() { LABELS+=("$1"); ENDPOINTS+=("$2"); BODIES+=("$3"); TIMEOUTS+=("${4:-$TIMEOUT}"); }

add "profile:simone"           /profile "{\"url\":\"$P1\"}"
add "profile:lisa"             /profile "{\"url\":\"$P2\"}"
add "profile:eugene"           /profile "{\"url\":\"$P3\"}"
add "profile:francesco"        /profile "{\"url\":\"$P4\"}"
add "company:kruncher"         /company "{\"url\":\"$C1\"}"
add "company:freda-ab"         /company "{\"url\":\"$C2\"}"
add "extract:simone"           /extract "{\"url\":\"$P1\"}"
add "extract:lisa"             /extract "{\"url\":\"$P2\"}"
add "extract:eugene"           /extract "{\"url\":\"$P3\"}"
add "extract:francesco"        /extract "{\"url\":\"$P4\"}"
add "extract:kruncher"         /extract "{\"url\":\"$C1\"}"
# Full company scrapes every sub-tab sequentially (home + about/posts/jobs/
# people/insights/life ≈ 7 page loads × ~20-30s each), so it needs a much longer
# timeout than single pages — otherwise it always times out (~150s+).
add "extract:kruncher-full"    /extract "{\"url\":\"$C1\",\"full\":true}" 300
add "extract:freda-ab"         /extract "{\"url\":\"$C2\"}"

# ---------- workspace ----------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
RESULTS="$TMP/results.tsv"

# ---------- readiness check ----------
# Fail fast on a dead/half-open connection: --connect-timeout caps the TCP
# handshake, -m caps the whole call, and we retry a few times with visible
# progress so a transient blip doesn't look like an indefinite hang.
CONNECT_TO=8
MAX_TO=15
ATTEMPTS=5
status_json=""; login_state=""
for a in $(seq 1 "$ATTEMPTS"); do
  printf '  [%d/%d] probing %s/login/status … ' "$a" "$ATTEMPTS" "$BASE_URL"
  status_json="$(curl -s --connect-timeout "$CONNECT_TO" -m "$MAX_TO" \
    -H "X-API-Key: $API_KEY" "$BASE_URL/login/status" 2>/dev/null || true)"
  rc=$?
  login_state="$(printf '%s' "$status_json" | sed -n 's/.*"state":"\([^"]*\)".*/\1/p')"
  if [ -n "$login_state" ]; then
    echo "ok (state=$login_state)"
    break
  fi
  echo "no response (curl=$rc); retrying in 3s …"
  [ "$a" -lt "$ATTEMPTS" ] && sleep 3
done

if [ -z "$login_state" ]; then
  echo "ERROR: server unreachable at $BASE_URL after $ATTEMPTS attempts" >&2
  echo "       (last curl exit=$rc). Check the host/port, your network, or" >&2
  echo "       redeploy with ./deploy.sh. Aborting — no point hammering a dead box." >&2
  exit 1
fi

if [ "$login_state" != "logged_in" ]; then
  echo "WARN: server not logged_in (state='$login_state')." >&2
  echo "     /profile /company /extract will return 409. Continue? hit Ctrl-C now." >&2
  echo "     response: $status_json" >&2
  sleep 3
fi

# ---------- single request ----------
# Captures curl's full timing breakdown and writes a 12-field TSV row:
#   label endpoint http dns conn tls server download ttfb total bytes err
# Phases (computed from curl's cumulative timestamps — bash can't do float math,
# so awk does it):
#   dns      = time_namelookup            (DNS resolve)
#   conn     = time_connect - namelookup  (TCP handshake)
#   tls      = time_appconnect - connect  (TLS handshake; 0 for plain HTTP)
#   server   = time_starttransfer - pretransfer  (request sent → first byte:
#              server processing + any queueing behind the single worker thread)
#   download = time_total - starttransfer (first byte → last byte)
#   ttfb     = time_starttransfer         (absolute first-byte time)
#   total    = time_total                 (request → full response)
curl_err_str() {  # map a curl exit code → human label
  case "$1" in
    0)  echo "ok";;
    6)  echo "dns_resolve_failed";;
    7)  echo "connect_failed/refused";;
    28) echo "timeout";;
    52) echo "empty_reply_from_server";;
    56) echo "recv_error";;
    *)  echo "curl_err_$1";;
  esac
}

do_request() {
  local idx="$1" label="$2" endpoint="$3" body="$4" req_to="${5:-$TIMEOUT}"
  local out rc http nl conn app pre start total size err=""
  # --connect-timeout lets a stuck TCP handshake fail fast instead of
  # monopolizing a worker slot for the full request timeout.
  out="$(curl -s --connect-timeout "$CONNECT_TO" -m "$req_to" -o /dev/null \
        -H "Content-Type: application/json" \
        -H "X-API-Key: $API_KEY" \
        -w '%{http_code}\t%{time_namelookup}\t%{time_connect}\t%{time_appconnect}\t%{time_pretransfer}\t%{time_starttransfer}\t%{time_total}\t%{size_download}' \
        -X POST "$BASE_URL$endpoint" -d "$body" 2>/dev/null)"
  rc=$?
  # Parse the 8 -w fields; on a hard connect failure curl may emit partial/empty
  # output, so zero-fill anything missing.
  IFS=$'\t' read -r http nl conn app pre start total size <<< "$out"
  http="${http:-000}"; nl="${nl:-0}"; conn="${conn:-0}"; app="${app:-0}"
  pre="${pre:-0}"; start="${start:-0}"; total="${total:-0}"; size="${size:-0}"
  [ "$rc" -ne 0 ] && err="$(curl_err_str "$rc")"
  # Compute the 5 phases via awk (one call, returns "dns tcp tls srv dl").
  local phases
  phases="$(awk -v nl="$nl" -v cn="$conn" -v ap="$app" -v pr="$pre" \
                -v st="$start" -v tot="$total" 'BEGIN{
    d=nl+0; t=(cn+0)-(nl+0); if(t<0)t=0
    s=(ap+0)-(cn+0); if(s<0)s=0
    if(st+0==0 && tot+0>0){ r=(tot+0)-(pr+0); if(r<0)r=0; l=0 }   # never got a byte: it was all server wait
    else { r=(st+0)-(pr+0); if(r<0)r=0; l=(tot+0)-(st+0); if(l<0)l=0 }
    printf "%.3f %.3f %.3f %.3f %.3f", d, t, s, r, l
  }')"
  local dns tcp tls srv dl
  read -r dns tcp tls srv dl <<< "$phases"
  # Live progress line shows WHERE the time went (conn/server/download/total).
  if [ -n "$err" ]; then
    printf '[FAIL] #%s %s → %s curl=%s(%s) conn=%ss server=%ss dl=%ss total=%ss bytes=%s\n' \
      "$idx" "$label" "$endpoint" "$rc" "$err" "$tcp" "$srv" "$dl" "$total" "$size"
  elif [ "$http" = "200" ]; then
    printf '[ ok ] #%s %s → %s http=%s conn=%ss server=%ss dl=%ss total=%ss bytes=%s\n' \
      "$idx" "$label" "$endpoint" "$http" "$tcp" "$srv" "$dl" "$total" "$size"
  else
    printf '[HTTP] #%s %s → %s http=%s conn=%ss server=%ss dl=%ss total=%ss bytes=%s\n' \
      "$idx" "$label" "$endpoint" "$http" "$tcp" "$srv" "$dl" "$total" "$size"
  fi
  # 12-field TSV row (ttfb kept at col 9 for completeness; report skips it).
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$label" "$endpoint" "$http" "$dns" "$tcp" "$tls" "$srv" "$dl" \
    "$start" "$total" "$size" "${err:-}" > "$TMP/job_$idx.tsv"
}

# ---------- plan ----------
TOTAL_REQS=$(( REPEATS * ${#LABELS[@]} ))
worst=$(( TOTAL_REQS * TIMEOUT ))
echo
echo "== stress run: $TOTAL_REQS requests (${#LABELS[@]} targets × $REPEATS repeats) =="
echo "   concurrency=$CONCURRENCY   per-request timeout=${TIMEOUT}s"
echo "   server serializes on ONE worker thread → effective order ≈ sequential;"
echo "   wall-time worst case ≈ ${worst}s (tune REPEATS/TIMEOUT if too long)."
echo "   live progress: [>>] dispatched · [ ok ]/[HTTP]/[FAIL] result · … heartbeat"
echo

# ---------- concurrency pool (fifo semaphore) ----------
sem="/tmp/stress.sem.$$"; mkfifo "$sem"; exec 9<>"$sem"; rm "$sem"
for ((i=0; i<CONCURRENCY; i++)); do echo >&9; done

# Heartbeat: print elapsed + completed count every 10s so a slow batch never
# looks like a hang. Killed after the request jobs finish.
RUN_T0=$(date +%s)
(
  while :; do
    sleep 10
    d=$(ls "$TMP"/job_*.tsv 2>/dev/null | wc -l | tr -d ' ')
    printf '  … %ss elapsed, %s/%s completed\n' "$(( $(date +%s) - RUN_T0 ))" "$d" "$TOTAL_REQS"
  done
) & HB=$!

pids=()
idx=0
for ((r=1; r<=REPEATS; r++)); do
  for t in "${!LABELS[@]}"; do
    idx=$((idx+1))
    read -r _ <&9                  # acquire a slot (blocks if pool full → shows backpressure)
    printf '[>>] #%s/%s dispatch %s %s (to=%ss)\n' "$idx" "$TOTAL_REQS" "${LABELS[$t]}" "${ENDPOINTS[$t]}" "${TIMEOUTS[$t]}"
    {
      do_request "$idx" "${LABELS[$t]}" "${ENDPOINTS[$t]}" "${BODIES[$t]}" "${TIMEOUTS[$t]}"
      echo >&9                     # release the slot
    } & pids+=($!)
  done
done
for p in "${pids[@]}"; do wait "$p"; done   # wait only on request jobs, not the heartbeat
kill "$HB" 2>/dev/null || true

echo
echo "== run finished in $(( $(date +%s) - RUN_T0 ))s =="

# ---------- gather results ----------
for f in "$TMP"/job_*.tsv; do [ -e "$f" ] && cat "$f"; done | sort -t_ -k2 -n > "$RESULTS"
TOTAL_REQS=$(wc -l < "$RESULTS" | tr -d ' ')

# ---------- stats helpers ----------
# TSV schema (12 cols): label endpoint http dns conn tls server download ttfb total bytes err
# total = col 10; err = col 12.
fmt() { awk -v x="$1" 'BEGIN{ printf "%.3f", x }'; }

stats_for() {
  # $1 = label or empty for "all"
  local label="$1" f="$TMP/vals" n min max med mean p95 ppos ok
  if [ -n "$label" ]; then
    awk -F'\t' -v L="$label" '$1==L{print $10}' "$RESULTS"
  else
    awk -F'\t' '{print $10}' "$RESULTS"
  fi | sort -n > "$f"
  n=$(wc -l < "$f" | tr -d ' ')
  [ "$n" -eq 0 ] && { echo "0	0	0.000	0.000	0.000	0.000	0.000"; return; }
  min=$(sed -n 1p "$f")
  max=$(sed -n "${n}p" "$f")
  med=$(sed -n "$(( (n+1)/2 ))p" "$f")
  ppos=$(awk -v n="$n" 'BEGIN{p=int(0.95*n+0.999); if(p<1)p=1; if(p>n)p=n; print p}')
  p95=$(sed -n "${ppos}p" "$f")
  mean=$(awk '{s+=$1}END{printf "%.3f",s/NR}' "$f")
  if [ -n "$label" ]; then
    ok=$(awk -F'\t' -v L="$label" '$1==L && $3==200 && $12==""{n++}END{print n+0}' "$RESULTS")
  else
    ok=$(awk -F'\t' '$3==200 && $12==""{n++}END{print n+0}' "$RESULTS")
  fi
  echo "$n	$ok	$(fmt "$min")	$(fmt "$med")	$mean	$(fmt "$max")	$(fmt "$p95")"
}

# ---------- markdown report ----------
{
echo "# LinkedIn scraper — stress test report"
echo
echo "- **Generated:** $(date '+%Y-%m-%d %H:%M:%S %z')"
echo "- **Base URL:** \`$BASE_URL\`"
echo "- **Login state at start:** \`${login_state:-unknown}\`"
echo "- **Concurrency:** $CONCURRENCY in-flight workers (server serializes via one browser-worker thread)"
echo "- **Repeats per target:** $REPEATS"
echo "- **Per-request timeout:** ${TIMEOUT}s"
echo "- **Total requests:** $TOTAL_REQS"
echo
echo "> Note: the API processes requests one at a time on a single worker thread."
echo "> Concurrency > 1 therefore measures queueing latency / backpressure, not throughput."
echo
echo "## Per-request results"
echo
echo "Phase breakdown (seconds): **DNS** → **Conn** (TCP) → **TLS** → **Server** (request sent → first byte: server processing + any queueing on the single worker thread) → **DL** (first byte → last byte) → **Total** (request → full response)."
echo
echo "| # | Target | Endpoint | HTTP | DNS | Conn | TLS | Server | DL | Total | Bytes | Note |"
echo "|--:|--------|----------|-----:|----:|-----:|-------:|---:|------:|------:|------|"
  awk -F'\t' '{
    note = ($12=="") ? "ok" : $12
    printf "| %d | %s | %s | %s | %.3f | %.3f | %.3f | %.3f | %.3f | %.3f | %s | %s |\n", \
      NR, $1, $2, $3, $4, $5, $6, $7, $8, $10, $11, note
  }' "$RESULTS"
echo
echo "## Aggregate by target"
echo
echo "| Target | N | OK | Success | Min | Median | Mean | Max | p95 |"
echo "|--------|--:|--:|--------:|----:|-------:|-----:|----:|----:|"
for t in "${!LABELS[@]}"; do
  IFS=$'\t' read -r n ok min med mean max p95 < <(stats_for "${LABELS[$t]}")
  sr=$(awk -v o="$ok" -v n="$n" 'BEGIN{printf "%.0f%%", (n? o*100/n : 0)}')
  printf '| %s | %s | %s | %s | %s | %s | %s | %s | %s |\n' \
    "${LABELS[$t]}" "$n" "$ok" "$sr" "$min" "$med" "$mean" "$max" "$p95"
done
echo
echo "## Overall"
echo
IFS=$'\t' read -r n ok min med mean max p95 < <(stats_for "")
sr=$(awk -v o="$ok" -v n="$n" 'BEGIN{printf "%.0f%%", (n? o*100/n : 0)}')
echo "| Requests | OK | Success | Min | Median | Mean | Max | p95 |"
echo "|--:|--:|--------:|----:|-------:|-----:|----:|----:|"
printf '| %s | %s | %s | %s | %s | %s | %s | %s |\n' \
  "$n" "$ok" "$sr" "$min" "$med" "$mean" "$max" "$p95"
echo
echo "Raw TSV columns: label, endpoint, http, dns, conn, tls, server, download, ttfb, total, bytes, err — in \`$RESULTS\` (temp)."
} > "$OUT"

echo
echo "Done. $TOTAL_REQS requests → $OUT"
