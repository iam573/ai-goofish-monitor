#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR}"
COMPOSE_CMD="${COMPOSE_CMD:-docker-compose}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yaml}"
SERVICE="${SERVICE:-app}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
LOG_LINES="${LOG_LINES:-120}"
DOCKER_USE_SUDO="${DOCKER_USE_SUDO:-auto}"

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
  DOCKER_USE_SUDO    Use sudo for docker-compose: auto, 1, or 0. Default: auto.
  HEALTH_URL         Health check URL. Default: http://127.0.0.1:8000/health
  LOG_LINES          Lines to show after startup. Default: 120

Notes:
  On NAS systems, Docker often requires root while git SSH keys belong to your
  normal user. This script keeps git pull under the original sudo user and runs
  docker-compose with root/sudo when needed.
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

run_git_pull() {
  if [ "$(id -u)" -eq 0 ] && [ "${SUDO_USER:-}" ] && [ "${SUDO_USER:-}" != "root" ]; then
    command -v sudo >/dev/null 2>&1 || die "sudo is required to run git as ${SUDO_USER}."
    log "Running as ${SUDO_USER}: git pull --ff-only"
    sudo -H -u "$SUDO_USER" sh -c 'cd "$1" && git pull --ff-only' sh "$PROJECT_DIR"
  else
    run git pull --ff-only
  fi
}

should_sudo_docker() {
  case "$DOCKER_USE_SUDO" in
    1|true|yes)
      return 0
      ;;
    0|false|no)
      return 1
      ;;
    auto)
      [ "$(id -u)" -ne 0 ]
      return
      ;;
    *)
      die "Invalid DOCKER_USE_SUDO value: ${DOCKER_USE_SUDO}. Use auto, 1, or 0."
      ;;
  esac
}

run_compose() {
  if should_sudo_docker; then
    command -v sudo >/dev/null 2>&1 || die "sudo is required for docker-compose."
    run sudo "$COMPOSE_CMD" -f "$COMPOSE_FILE" "$@"
  else
    run "$COMPOSE_CMD" -f "$COMPOSE_FILE" "$@"
  fi
}

wait_for_health() {
  if ! command -v curl >/dev/null 2>&1; then
    log "curl is not available; skipping health check."
    return 0
  fi

  log "Waiting for health check: ${HEALTH_URL}"
  i=0
  while [ "$i" -lt 30 ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      log "Health check passed."
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done

  log "Health check did not pass within 60 seconds. Recent logs may explain why."
  return 1
}

while [ "$#" -gt 0 ]; do
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
      [ "$#" -ge 2 ] || die "--service requires a value."
      SERVICE="$2"
      shift 2
      ;;
    --compose-file)
      [ "$#" -ge 2 ] || die "--compose-file requires a value."
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

cd "$PROJECT_DIR"

command -v git >/dev/null 2>&1 || die "git is not installed."
command -v "$COMPOSE_CMD" >/dev/null 2>&1 || die "$COMPOSE_CMD is not installed."
[ -f "$COMPOSE_FILE" ] || die "Compose file not found: $COMPOSE_FILE"
[ -f ".env" ] || die "Missing .env. Copy .env.example to .env and fill required values."
[ -f "config.json" ] || die "Missing config.json."

log "Project directory: $PROJECT_DIR"
log "Compose file: $COMPOSE_FILE"
log "Service: $SERVICE"

if [ "$DO_PULL" = "1" ]; then
  run_git_pull
else
  log "Skipping git pull."
fi

if [ "$NO_CACHE" = "1" ]; then
  run_compose build --no-cache "$SERVICE"
else
  run_compose build "$SERVICE"
fi

run_compose up -d --remove-orphans "$SERVICE"
run_compose ps

if ! wait_for_health; then
  run_compose logs --tail="$LOG_LINES" "$SERVICE"
  exit 1
fi

run_compose logs --tail="$LOG_LINES" "$SERVICE"

if [ "$FOLLOW_LOGS" = "1" ]; then
  run_compose logs -f --tail="$LOG_LINES" "$SERVICE"
fi

log "Deploy finished."
