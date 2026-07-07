#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
COMPOSE_CMD="${COMPOSE_CMD:-docker-compose}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
SERVICE="${SERVICE:-app}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
LOG_LINES="${LOG_LINES:-120}"

DO_PULL=1
NO_CACHE=0
FOLLOW_LOGS=0

usage() {
  cat <<'EOF'
Usage: ./deploy-nas.sh [options]

Update code, rebuild the Docker image, and restart the NAS deployment.

Options:
  --no-pull          Skip git pull.
  --no-cache         Rebuild Docker image without cache.
  --follow-logs      Follow docker-compose logs after startup.
  --service NAME     Compose service to deploy. Default: app.
  --compose-file F   Compose file path. Default: docker-compose.yaml.
  -h, --help         Show this help.

Environment:
  PROJECT_DIR        Project directory. Default: script directory.
  COMPOSE_CMD        Compose command. Default: docker-compose.
  HEALTH_URL         Health check URL. Default: http://127.0.0.1:8000/health
  LOG_LINES          Lines to show after startup. Default: 120
EOF
}

log() {
  printf '\n[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  printf '\nERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  log "Running: $*"
  "$@"
}

wait_for_health() {
  if ! command -v curl >/dev/null 2>&1; then
    log "curl is not available; skipping health check."
    return 0
  fi

  log "Waiting for health check: ${HEALTH_URL}"
  for _ in $(seq 1 30); do
    if curl -fsS "${HEALTH_URL}" >/dev/null 2>&1; then
      log "Health check passed."
      return 0
    fi
    sleep 2
  done

  log "Health check did not pass within 60 seconds. Recent logs may explain why."
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull)
      DO_PULL=0
      shift
      ;;
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --follow-logs)
      FOLLOW_LOGS=1
      shift
      ;;
    --service)
      [[ $# -ge 2 ]] || die "--service requires a value."
      SERVICE="$2"
      shift 2
      ;;
    --compose-file)
      [[ $# -ge 2 ]] || die "--compose-file requires a value."
      COMPOSE_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

cd "${PROJECT_DIR}"

command -v git >/dev/null 2>&1 || die "git is not installed."
command -v "${COMPOSE_CMD}" >/dev/null 2>&1 || die "${COMPOSE_CMD} is not installed."
[[ -f "${COMPOSE_FILE}" ]] || die "Compose file not found: ${COMPOSE_FILE}"
[[ -f ".env" ]] || die "Missing .env. Copy .env.example to .env and fill required values."
[[ -f "config.json" ]] || die "Missing config.json."

log "Project directory: ${PROJECT_DIR}"
log "Compose file: ${COMPOSE_FILE}"
log "Service: ${SERVICE}"

if [[ "${DO_PULL}" == "1" ]]; then
  run git pull --ff-only
else
  log "Skipping git pull."
fi

if [[ "${NO_CACHE}" == "1" ]]; then
  run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" build --no-cache "${SERVICE}"
else
  run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" build "${SERVICE}"
fi

run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" up -d --remove-orphans "${SERVICE}"
run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" ps

if ! wait_for_health; then
  run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" logs --tail="${LOG_LINES}" "${SERVICE}"
  exit 1
fi

run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" logs --tail="${LOG_LINES}" "${SERVICE}"

if [[ "${FOLLOW_LOGS}" == "1" ]]; then
  run "${COMPOSE_CMD}" -f "${COMPOSE_FILE}" logs -f --tail="${LOG_LINES}" "${SERVICE}"
fi

log "Deploy finished."
