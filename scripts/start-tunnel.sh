#!/usr/bin/env bash
# Expose the local backend to the Vercel site in one step.
#
# Starts a Cloudflare quick tunnel to localhost:8000, waits until its new
# address answers, points the Vercel project's NEXT_PUBLIC_API_BASE at it and
# redeploys production. The Mac is kept awake while the tunnel runs, because
# sleep is what kills quick tunnels. Ctrl+C stops the tunnel.
#
# Needs: cloudflared, the Vercel CLI logged in, and the backend running on
# port 8000. Run from anywhere:  bash scripts/start-tunnel.sh

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SITE="https://delta-f1.vercel.app"
LOG="$(mktemp -t delta-tunnel)"

if ! curl -fs -m 5 http://127.0.0.1:8000/api/health >/dev/null; then
  echo "The backend is not running on port 8000. Start it first:"
  echo "  cd \"$REPO/backend\" && nohup .venv/bin/uvicorn app.main:app --port 8000 > backend.log 2>&1 &"
  exit 1
fi

if pgrep -f "cloudflared tunnel.*localhost:8000" >/dev/null; then
  echo "A tunnel to port 8000 is already running (pids: $(pgrep -f 'cloudflared tunnel.*localhost:8000' | tr '\n' ' '))."
  echo "Stop it first, or keep using it."
  exit 1
fi

echo "Starting the tunnel (log: $LOG)..."
# caffeinate -i stops the Mac idle-sleeping for as long as the tunnel runs.
caffeinate -i cloudflared tunnel --edge-ip-version 4 --url http://localhost:8000 >"$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'echo; echo "Stopping the tunnel."; kill "$TUNNEL_PID" 2>/dev/null || true' EXIT INT TERM

URL=""
for _ in $(seq 1 60); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "cloudflared exited:"; tail -5 "$LOG"; exit 1
  fi
  sleep 1
done
[ -n "$URL" ] || { echo "No tunnel address after 60 s:"; tail -5 "$LOG"; exit 1; }
echo "Tunnel address: $URL"

# A brand-new address can take a few seconds to resolve.
echo -n "Waiting for it to answer"
for _ in $(seq 1 40); do
  if curl -fs -m 10 "$URL/api/health" >/dev/null 2>&1; then echo " ok"; break; fi
  echo -n "."; sleep 3
done
curl -fs -m 10 "$URL/api/health" >/dev/null || { echo; echo "The tunnel never answered."; exit 1; }

cd "$REPO"
echo "Pointing Vercel at the new address..."
vercel env rm NEXT_PUBLIC_API_BASE production --yes >/dev/null 2>&1 || true
printf %s "$URL" | vercel env add NEXT_PUBLIC_API_BASE production >/dev/null

echo "Redeploying production (about a minute)..."
CURRENT="$(vercel ls delta --prod 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.vercel\.app' | head -1)"
vercel redeploy "$CURRENT" --target production >/dev/null 2>&1

echo
echo "Live: $SITE now uses $URL"
echo "Keep this window open. Ctrl+C stops the tunnel (and the site's data)."
wait "$TUNNEL_PID"
