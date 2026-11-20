#!/usr/bin/env bash
# Pull latest code, rebuild the image, restart the container, wait for health.
#   bash /opt/jev-nav/deploy/scripts/deploy.sh
# Rollback: see the hint printed at the end.
set -euo pipefail

APP_DIR=/opt/jev-nav
cd "$APP_DIR"
[ -f "$APP_DIR/.env" ] || { echo "Missing $APP_DIR/.env (copy deploy/env.example)" >&2; exit 1; }

# Keep the current image as :previous for rollback
if docker image inspect jev-nav:latest >/dev/null 2>&1; then
  docker tag jev-nav:latest jev-nav:previous
fi

git pull --ff-only
git log -1 --format='deploying %h %s'

cd "$APP_DIR/deploy"
DC=(docker compose --env-file "$APP_DIR/.env")
"${DC[@]}" build
"${DC[@]}" up -d --remove-orphans

echo "Waiting for health checks (up to 3 min)..."
for _ in $(seq 1 36); do
  bad=0
  for c in jev; do
    st=$(docker inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo missing)
    [ "$st" = healthy ] || bad=1
  done
  [ "$bad" -eq 0 ] && break
  sleep 5
done

"${DC[@]}" ps
if [ "$bad" -ne 0 ]; then
  echo "The container is not healthy. Logs: docker compose logs --tail=100 jev" >&2
  exit 1
fi
docker image prune -f >/dev/null

cat <<'MSG'

OK. Rollback to the previous image:
  docker tag jev-nav:previous jev-nav:latest
  cd /opt/jev-nav/deploy && docker compose --env-file /opt/jev-nav/.env up -d --no-build
(also `git checkout <old-commit>` if you want the source to match; git pull will move it forward again)
MSG
