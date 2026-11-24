#!/usr/bin/env bash
#
# ChatterMate — one-command installer for a fresh Ubuntu 22.04 / 24.04 VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/NEW_OWNER/chattermate/main/install.sh \
#     | sudo bash -s -- [your-domain.com]
#
# Or, after cloning the repo:
#
#   sudo bash install.sh [your-domain.com]
#
# What it does, with no manual step:
#   1. Installs system deps (Python 3.12, Node.js 20, nginx, Redis,
#      PostgreSQL 16 + pgvector, certbot).
#   2. Clones (or updates) this repo into /opt/chattermate.
#   3. Creates the Postgres database/user and enables the vector extension.
#   4. Generates backend/.env with random secrets (nothing placeholder).
#   5. Creates the Python venv, installs requirements, runs DB migrations.
#   6. Builds the frontend + the embeddable widget bundle.
#   7. Installs systemd services (auto-start on boot) and an nginx site,
#      with free HTTPS via certbot when a real domain is given.
#   8. Prints the URLs — open /setup once and create your admin account.
#
# Idempotent: safe to re-run. It never overwrites backend/.env (your secrets),
# never drops the database, and refuses to pull over uncommitted local changes
# unless --force is given.
#
# Options (flags or env vars):
#   --domain DOMAIN   Public domain, e.g. support.example.com (or CM_DOMAIN).
#                     Without one, the server's public IP is used over plain HTTP.
#   --repo URL        Git repo to install from (default below, or CM_REPO_URL).
#   --dir PATH        Install directory (default /opt/chattermate, or CM_INSTALL_DIR).
#   --branch BRANCH   Git branch to check out (default main, or CM_BRANCH).
#   --no-ssl          Skip certbot even when a domain is given.
#   --force           Allow `git pull` over uncommitted changes (they are lost).
#   --help            Show this help and exit.
#
# Requirements: Ubuntu 22.04 or 24.04, ~2 GB RAM (a swap file is added if the
# box has less), a public IP, and outbound internet. Run as root or via sudo.
#
set -euo pipefail

# ---------------------------------------------------------------- config ----
DEFAULT_REPO_URL="https://github.com/NEW_OWNER/chattermate.git"
# TODO: replace NEW_OWNER/chattermate with the real repo before publishing.
REPO_URL="${CM_REPO_URL:-$DEFAULT_REPO_URL}"
INSTALL_DIR="${CM_INSTALL_DIR:-/opt/chattermate}"
BRANCH="${CM_BRANCH:-main}"
DOMAIN="${CM_DOMAIN:-}"
NO_SSL=0
FORCE=0

APP_USER="chattermate"
DB_NAME="chattermate"
DB_USER="chattermate"
LOG_FILE="/var/log/chattermate-install.log"
STATE_FILE="$INSTALL_DIR/.install-state"

usage() {
    # Plain heredoc, not `sed $0`: under `curl ... | sudo bash -s -- --help`
    # $0 is `bash`, so reading the script file would print nothing useful.
    cat <<'EOF'
ChatterMate one-command installer for a fresh Ubuntu 22.04/24.04 VPS.

Usage:
  curl -fsSL <raw-install.sh-url> | sudo bash -s -- [your-domain.com]
  sudo bash install.sh [your-domain.com]

Options:
  --domain DOMAIN   Public domain, e.g. support.example.com (or CM_DOMAIN).
                    Without one, the server's public IP is used over HTTP.
  --repo URL        Git repo to install from (default: see DEFAULT_REPO_URL).
  --dir PATH        Install directory (default /opt/chattermate).
  --branch BRANCH   Git branch to check out (default main).
  --no-ssl          Skip certbot even when a domain is given.
  --force           Discard uncommitted changes in the install dir (reset --hard).
  --help            Show this help and exit.
EOF
}

# ------------------------------------------------------------ arguments ----
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --domain=*) DOMAIN="${1#--domain=}"; shift ;;
        --repo) REPO_URL="$2"; shift 2 ;;
        --repo=*) REPO_URL="${1#--repo=}"; shift ;;
        --dir) INSTALL_DIR="$2"; STATE_FILE="$INSTALL_DIR/.install-state"; shift 2 ;;
        --dir=*) INSTALL_DIR="${1#--dir=}"; STATE_FILE="$INSTALL_DIR/.install-state"; shift ;;
        --branch) BRANCH="$2"; shift 2 ;;
        --branch=*) BRANCH="${1#--branch=}"; shift ;;
        --no-ssl) NO_SSL=1; shift ;;
        --force) FORCE=1; shift ;;
        --help|-h) usage; exit 0 ;;
        --) shift; break ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
        *)  if [ -z "$DOMAIN" ]; then DOMAIN="$1"; else
                echo "Unexpected argument: $1" >&2; usage >&2; exit 2; fi
            shift ;;
    esac
done

# ---------------------------------------------------------------- helpers ---
say()  { printf '\n==> %s\n' "$*" | tee -a "$LOG_FILE"; }
ok()   { printf '    ✓ %s\n' "$*" | tee -a "$LOG_FILE"; }
warn() { printf '    ! WARNING: %s\n' "$*" | tee -a "$LOG_FILE"; }
die()  { printf '\n✗ ERROR: %s\n' "$*" | tee -a "$LOG_FILE" >&2; exit 1; }

# Render a template file, replacing __TOKEN__ placeholders with values.
# Python, not sed: values containing &, #, \ or / would corrupt a sed
# replacement string. Usage:
#   render_template SRC DEST TOKEN1 VALUE1 [TOKEN2 VALUE2 ...]
render_template() {
    local src="$1" dest="$2"; shift 2
    python3 - "$src" "$dest" "$@" <<'PY'
import sys
src, dest, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
text = open(src).read()
for i in range(0, len(pairs), 2):
    text = text.replace("__%s__" % pairs[i], pairs[i + 1])
open(dest, "w").write(text)
PY
}

# Run a command, echoing it to the log. Output goes to the log, not the screen,
# so the terminal stays readable; the tail is shown if it fails.
run_quiet() {
    echo "\$ $*" >> "$LOG_FILE"
    if ! "$@" >> "$LOG_FILE" 2>&1; then
        echo "--- last 25 lines of $LOG_FILE ---" >&2
        tail -n 25 "$LOG_FILE" >&2
        die "command failed: $*"
    fi
}

is_ip() { case "$1" in *[!0-9.]*) return 1;; *) return 0;; esac; }

rand_hex() { openssl rand -hex "${1:-32}"; }

# Fernet key, base64-encoded a second time (the format backend/.env.example
# documents): 60 chars ending in '='. Uses only openssl, no Python needed.
rand_fernet_key() {
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n' | base64 | tr -d '\n'
    echo
}

# ------------------------------------------------------------------ root ---
if [ "$(id -u)" -ne 0 ]; then
    die "run as root (or with sudo). Example: curl ... | sudo bash -s -- $DOMAIN"
fi
command -v sudo >/dev/null 2>&1 || die "sudo is required (used for the postgres and app users) but is not installed"
# Fail fast on the placeholder repo instead of a confusing curl 404 / clone
# failure halfway through. Replace NEW_OWNER/chattermate with the real repo.
case "$REPO_URL" in
    *NEW_OWNER*) die "REPO_URL still points at the NEW_OWNER placeholder — set --repo (or CM_REPO_URL) to the real repo, e.g. https://github.com/<owner>/<repo>.git" ;;
esac
touch "$LOG_FILE" 2>/dev/null || LOG_FILE="/tmp/chattermate-install.log"
mkdir -p "$(dirname "$LOG_FILE")"; touch "$LOG_FILE"

say "ChatterMate one-command install — repo $REPO_URL (branch $BRANCH)"

# -------------------------------------------------------------------- OS ---
if [ -r /etc/os-release ]; then . /etc/os-release; else die "cannot read /etc/os-release"; fi
case "${ID:-}${VERSION_ID:-}" in
    ubuntu22.04|ubuntu24.04) ok "Ubuntu $VERSION_ID (supported)" ;;
    *) warn "unsupported OS ($PRETTY_NAME) — continuing; Ubuntu 22.04/24.04 is what this installer is tested on" ;;
esac
export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------- public hostname ---
# A domain must look like a hostname (labels of letters/digits/hyphens, at
# least one dot). IPv6 and malformed values are rejected: they would produce
# an invalid server_name and a certbot failure.
if [ -n "$DOMAIN" ]; then
    if is_ip "$DOMAIN"; then
        PUBLIC_HOST="$DOMAIN"
    elif printf '%s' "$DOMAIN" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'; then
        PUBLIC_HOST="$DOMAIN"
    else
        die "--domain '$DOMAIN' is not a valid domain or IPv4 address"
    fi
else
    PUBLIC_HOST="$(curl -fsSL --max-time 10 https://ifconfig.me 2>/dev/null || true)"
    if [ -z "$PUBLIC_HOST" ]; then
        # hostname -I can list private/cloud-internal addresses first; prefer
        # a public IPv4 address, fall back to the first address it reports.
        PUBLIC_HOST="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' \
            | grep -vE '^(10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|127\.)' | head -1 \
            || hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    [ -n "$PUBLIC_HOST" ] || die "could not determine the server IP — pass a domain: bash install.sh your-domain.com"
    warn "no domain given — the app will be served over plain HTTP at $PUBLIC_HOST"
fi

# A real domain (not an IP) is the only case where HTTPS is attempted.
USE_SSL=0
if [ "$NO_SSL" -eq 0 ] && [ -n "$DOMAIN" ] && ! is_ip "$DOMAIN"; then USE_SSL=1; fi
if [ "$USE_SSL" -eq 1 ]; then
    SCHEME="https"; WS_SCHEME="wss"
else
    SCHEME="http"; WS_SCHEME="ws"
fi
PUBLIC_URL="$SCHEME://$PUBLIC_HOST"

# ------------------------------------------------------------ swap (2 GB) ---
TOTAL_MEM_KB="$(awk '/MemTotal/{print $2}' /proc/meminfo)"
if [ "${TOTAL_MEM_KB:-0}" -lt 2000000 ] && ! swapon --show 2>/dev/null | grep -q .; then
    say "Less than 2 GB RAM — adding a 2 GB swap file (pip/npm need the headroom)"
    if [ ! -f /swapfile ]; then
        run_quiet fallocate -l 2G /swapfile || run_quiet dd if=/dev/zero of=/swapfile bs=1M count=2048
        chmod 600 /swapfile
        run_quiet mkswap /swapfile
    fi
    # fstab entry is independent of whether the file already existed: a
    # pre-existing swapfile without an entry would lose swap on reboot.
    grep -q '^/swapfile' /etc/fstab 2>/dev/null || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    swapon /swapfile 2>/dev/null || true
    ok "swap ready"
fi

# ------------------------------------------------------- system packages ---
say "Installing system packages (Python 3.12, Node.js 20, nginx, Redis, PostgreSQL 16 + pgvector)"
run_quiet apt-get update -qq
run_quiet apt-get install -y -qq curl git ca-certificates gnupg lsb-release \
    software-properties-common apt-transport-https unzip nginx redis-server \
    certbot python3-certbot-nginx ufw openssl

# Python 3.12: native on 24.04, deadsnakes PPA on 22.04.
if ! command -v python3.12 >/dev/null 2>&1; then
    say "Python 3.12 not present — adding the deadsnakes PPA"
    run_quiet add-apt-repository -y ppa:deadsnakes/ppa
    run_quiet apt-get update -qq
fi
run_quiet apt-get install -y -qq python3.12 python3.12-venv python3.12-dev

# Node.js 20 (the repo's CI pins Node 20). Vite 6 needs Node >= 20.19, so
# anything older than that — including early 20.x — is upgraded. NodeSource
# ships the latest 20.x, which satisfies the requirement.
need_node=1
if command -v node >/dev/null 2>&1; then
    INSTALLED_NODE="$(node --version | sed 's/^v//')"
    if [ "$(printf '%s\n%s\n' "20.19.0" "$INSTALLED_NODE" | sort -V | head -1)" = "20.19.0" ]; then
        need_node=0
    fi
fi
if [ "$need_node" -eq 1 ]; then
    say "Installing Node.js 20 from NodeSource"
    curl -fsSL https://deb.nodesource.com/setup_20.x -o /tmp/nodesetup_20.x.sh
    run_quiet bash /tmp/nodesetup_20.x.sh
    run_quiet apt-get install -y -qq nodejs
fi
ok "python $(python3.12 --version 2>&1), node $(node --version), npm $(npm --version)"

# PostgreSQL 16 + pgvector from the official PGDG repo (works on 22.04 and 24.04).
if ! apt-cache policy postgresql-16 2>/dev/null | grep -q 'apt.postgresql.org'; then
    say "Adding the PostgreSQL (PGDG) apt repository"
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg
    echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list
    run_quiet apt-get update -qq
fi
run_quiet apt-get install -y -qq postgresql-16 postgresql-16-pgvector
run_quiet systemctl enable --now postgresql redis-server
ok "PostgreSQL 16 + pgvector, Redis installed and running"

# ------------------------------------------------------------- firewall ----
# Unconditional: ufw records rules even while inactive, so enabling it later
# can never lock the app out. (Cloud-provider firewalls are out of scope.)
if command -v ufw >/dev/null 2>&1; then
    run_quiet ufw allow 80/tcp
    run_quiet ufw allow 443/tcp
    ok "ufw: allowed 80/tcp and 443/tcp"
fi

# ------------------------------------------------------------ app user -----
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$APP_USER"
    ok "created system user $APP_USER"
fi

# ------------------------------------------------------------ repo clone ---
say "Fetching the app code into $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    # The build regenerates these TRACKED files, so after the first install
    # the tree is always dirty here. Restore exactly these paths before the
    # dirty check — anything else dirty is a real operator edit.
    git checkout -- frontend/public/config.js backend/assets/widget.js backend/assets/widget.css 2>/dev/null || true
    if [ "$(git status --porcelain | grep -v '^??' | head -1)" ] && [ "$FORCE" -eq 0 ]; then
        die "$INSTALL_DIR has uncommitted changes — commit/stash them, or re-run with --force (their changes will be lost)"
    fi
    run_quiet git fetch origin
    run_quiet git checkout "$BRANCH"
    if [ "$FORCE" -eq 1 ]; then
        run_quiet git reset --hard "origin/$BRANCH"
    else
        run_quiet git pull --ff-only origin "$BRANCH"
    fi
    ok "updated existing checkout to $(git rev-parse --short HEAD)"
else
    if [ -e "$INSTALL_DIR" ]; then
        die "$INSTALL_DIR exists but is not a git checkout — move it aside and re-run"
    fi
    run_quiet git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    ok "cloned $(git rev-parse --short HEAD)"
fi
mkdir -p "$INSTALL_DIR/backend/uploads" "$INSTALL_DIR/backend/logs"
chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR"

# --------------------------------------------------------------- database --
say "Creating the database (user $DB_USER, database $DB_NAME, vector extension)"
# Reuse the password from an existing backend/.env on re-runs, so a re-install
# never locks the app out of its own database. Parsed with urllib, not sed:
# a password containing '@' would be truncated by a naive regex. Only
# alphanumeric passwords are reused — anything else would break the
# single-quoted SQL below, so it is regenerated and .env is updated instead.
DB_PASSWORD_CHANGED=0
if [ -f "$INSTALL_DIR/backend/.env" ]; then
    DB_PASSWORD="$(ENV_FILE="$INSTALL_DIR/backend/.env" python3 -c '
import os, urllib.parse
url = ""
for line in open(os.environ["ENV_FILE"], errors="ignore"):
    if line.startswith("DATABASE_URL="):
        url = line.strip().split("=", 1)[1]
        break
print(urllib.parse.urlparse(url).password or "")
' 2>/dev/null || true)"
    if [ -n "${DB_PASSWORD:-}" ] && ! printf '%s' "$DB_PASSWORD" | grep -Eq '^[A-Za-z0-9]+$'; then
        DB_PASSWORD=""; DB_PASSWORD_CHANGED=1
    fi
fi
if [ -z "${DB_PASSWORD:-}" ]; then DB_PASSWORD="$(rand_hex 16)"; fi
# The redirect is done by this script's shell, which runs as root;
# sudo only affects psql itself.
# shellcheck disable=SC2024
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL >> "$LOG_FILE" 2>&1
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER') THEN
    CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASSWORD';
  ELSE
    ALTER ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASSWORD';
  END IF;
END \$\$;
SELECT 'CREATE DATABASE $DB_NAME OWNER $DB_USER'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$DB_NAME') \gexec
\c $DB_NAME
CREATE EXTENSION IF NOT EXISTS vector;
SQL
ok "database ready"

# ------------------------------------------------------- backend secrets ---
say "Writing backend/.env (generated secrets — kept as-is on re-runs)"
ENV_FILE="$INSTALL_DIR/backend/.env"
if [ ! -f "$ENV_FILE" ]; then
    JWT_SECRET="$(rand_hex 32)"
    CONV_SECRET="$(rand_hex 32)"
    ENC_KEY="$(rand_fernet_key)"
    cat > "$ENV_FILE" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# BACK THIS FILE UP separately from the database: ENCRYPTION_KEY is the only
# thing that can decrypt stored API keys, chat history and agent memory.
# Losing it makes existing data unreadable; changing it does the same.
DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASSWORD@localhost:5432/$DB_NAME
CORS_ORIGINS=["$PUBLIC_URL"]
FRONTEND_URL=$PUBLIC_URL
BACKEND_URL=$PUBLIC_URL
VITE_WIDGET_URL=$PUBLIC_URL
WIDGET_FRAME_URL=
HELP_CENTER_PUBLIC_MODE=path
JWT_SECRET_KEY=$JWT_SECRET
JWT_ALGORITHM=HS256
CONVERSATION_SECRET_KEY=$CONV_SECRET
ENCRYPTION_KEY=$ENC_KEY
REDIS_ENABLED=true
REDIS_URL=redis://localhost:6379/0
ENVIRONMENT=production
EOF
    chmod 600 "$ENV_FILE"
    ok "backend/.env created (JWT + conversation + Fernet encryption keys generated)"
else
    ok "backend/.env already exists — keeping your secrets"
    # A re-run with a different domain must move the URL lines with it
    # (CORS, SSO callbacks and the widget all derive from BACKEND_URL);
    # secrets are never touched. If the DB password had to be regenerated
    # above, DATABASE_URL is repointed at the new one as well.
    python3 - "$ENV_FILE" "$PUBLIC_URL" "$DB_USER" "$DB_PASSWORD" "$DB_NAME" "$DB_PASSWORD_CHANGED" <<'PY'
import sys
path, public_url, db_user, db_password, db_name = sys.argv[1:6]
changed = sys.argv[6] == "1"
url_keys = {"CORS_ORIGINS": '["%s"]' % public_url,
            "FRONTEND_URL": public_url, "BACKEND_URL": public_url,
            "VITE_WIDGET_URL": public_url}
lines = open(path).read().splitlines()
out, seen = [], set()
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in url_keys:
        seen.add(key)
        out.append("%s=%s" % (key, url_keys[key]))
    elif key == "DATABASE_URL" and changed:
        out.append("DATABASE_URL=postgresql+psycopg://%s:%s@localhost:5432/%s"
                   % (db_user, db_password, db_name))
    else:
        out.append(line)
for key, value in url_keys.items():
    if key not in seen:
        out.append("%s=%s" % (key, value))
open(path, "w").write("\n".join(out) + "\n")
PY
    ok "backend/.env URL lines updated to $PUBLIC_URL"
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"

# ------------------------------------------------------- python backend ----
say "Creating the Python venv and installing backend requirements (this takes a few minutes)"
if [ ! -x "$INSTALL_DIR/backend/venv/bin/python" ]; then
    run_quiet sudo -u "$APP_USER" python3.12 -m venv "$INSTALL_DIR/backend/venv"
fi
run_quiet sudo -u "$APP_USER" "$INSTALL_DIR/backend/venv/bin/pip" install -q -U pip
run_quiet sudo -u "$APP_USER" "$INSTALL_DIR/backend/venv/bin/pip" install -q -r "$INSTALL_DIR/backend/requirements.txt"
ok "backend dependencies installed"

# Sanity-check the Fernet key with the real library before migrating.
# The backend refuses to start on a bad ENCRYPTION_KEY, so catch it here.
# Same decode the app does (see backend/app/core/encryption.py: load_key):
# the env var is a Fernet key base64-encoded a second time.
# Same as above: root's shell does the redirect.
# shellcheck disable=SC2024
if ! sudo -u "$APP_USER" env ENCRYPTION_KEY="$(grep -E '^ENCRYPTION_KEY=' "$ENV_FILE" | cut -d= -f2)" \
        "$INSTALL_DIR/backend/venv/bin/python" -c \
        "import base64, os; from cryptography.fernet import Fernet; Fernet(base64.b64decode(os.environ['ENCRYPTION_KEY']))" \
        >> "$LOG_FILE" 2>&1; then
    die "generated ENCRYPTION_KEY failed validation — delete backend/.env and re-run"
fi
ok "ENCRYPTION_KEY validated"
say "Running database migrations"
run_quiet sudo -u "$APP_USER" bash -c "cd '$INSTALL_DIR/backend' && './venv/bin/alembic' upgrade head"
ok "migrations applied"

# ------------------------------------------------------ frontend build -----
say "Building the frontend and the widget bundle (this takes a few minutes)"
cat > "$INSTALL_DIR/frontend/.env.prod" <<EOF
# Generated by install.sh — public URLs for this install.
VITE_API_URL=$PUBLIC_URL/api/v1
VITE_WS_URL=$WS_SCHEME://$PUBLIC_HOST
VITE_WIDGET_URL=$PUBLIC_URL
EOF
chown "$APP_USER:$APP_USER" "$INSTALL_DIR/frontend/.env.prod"
cd "$INSTALL_DIR/frontend"
if [ ! -d node_modules ] || [ package-lock.json -nt node_modules ]; then
    run_quiet sudo -u "$APP_USER" npm ci --no-audit --no-fund
fi
# Rebuild when the code changed since the last build (recorded in the state
# file), when dist/ is missing, or when the public URL changed: Vite inlines
# the VITE_* URLs at build time, so a domain change needs a fresh build even
# with identical code. A re-run with no changes skips the slow build.
BUILT_COMMIT="$(grep -E '^built_commit=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)"
RECORDED_URL="$(grep -E '^public_url=' "$STATE_FILE" 2>/dev/null | cut -d= -f2 || true)"
HEAD_COMMIT="$(cd "$INSTALL_DIR" && git rev-parse HEAD)"
if [ "$BUILT_COMMIT" != "$HEAD_COMMIT" ] || [ ! -d dist ] || [ "$RECORDED_URL" != "$PUBLIC_URL" ]; then
    run_quiet sudo -u "$APP_USER" npm run build:prod
    run_quiet sudo -u "$APP_USER" npm run build:widget:prod
    ok "frontend + widget built (dist/ and backend/assets/widget.js)"
else
    ok "frontend already built for $HEAD_COMMIT — skipping"
fi
[ -f "$INSTALL_DIR/backend/assets/widget.js" ] \
    || die "widget bundle missing after build (backend/assets/widget.js not found)"
chown -R "$APP_USER:$APP_USER" "$INSTALL_DIR/frontend/dist" "$INSTALL_DIR/backend/assets"

# ------------------------------------------------------ systemd services --
say "Installing systemd services (backend, auto-start on boot)"
render_template "$INSTALL_DIR/deploy/systemd/chattermate-backend.service" \
    /etc/systemd/system/chattermate-backend.service \
    INSTALL_DIR "$INSTALL_DIR" APP_USER "$APP_USER"
run_quiet systemctl daemon-reload
run_quiet systemctl enable chattermate-backend
run_quiet systemctl restart chattermate-backend
ok "chattermate-backend enabled and started"

# ---------------------------------------------------------------- nginx ----
say "Configuring nginx (frontend on /, API + widget on the backend)"
NGINX_SITE="/etc/nginx/sites-available/chattermate"
# If a certificate already exists for this domain, the site file is
# certbot-managed (443 block + redirect). Rewriting it from the port-80
# template would wipe HTTPS, so leave it alone and only reload.
CERT_EXISTS=0
if [ "$USE_SSL" -eq 1 ] && [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    CERT_EXISTS=1
fi
if [ "$CERT_EXISTS" -eq 0 ]; then
    render_template "$INSTALL_DIR/deploy/nginx/chattermate.conf.template" "$NGINX_SITE" \
        PUBLIC_HOST "$PUBLIC_HOST" INSTALL_DIR "$INSTALL_DIR"
    ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/chattermate
    rm -f /etc/nginx/sites-enabled/default
else
    say "HTTPS certificate already exists for $DOMAIN — keeping the certbot-managed nginx site"
fi
run_quiet nginx -t
run_quiet systemctl enable --now nginx
run_quiet systemctl reload nginx
ok "nginx serving $INSTALL_DIR/frontend/dist"

# ------------------------------------------------------------- HTTPS -------
# Best effort only: if certbot fails (DNS not pointing here yet, rate limit,
# no domain), the install still succeeds over HTTP and the admin can re-run
# `certbot --nginx -d <domain>` later.
if [ "$USE_SSL" -eq 1 ] && [ "$CERT_EXISTS" -eq 0 ]; then
    say "Requesting a free HTTPS certificate for $DOMAIN (best effort)"
    if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
            --register-unsafely-without-email --redirect >> "$LOG_FILE" 2>&1; then
        ok "HTTPS enabled for $DOMAIN"
    else
        warn "certbot failed — the app works over HTTP for now. Check DNS points at this server, then run: certbot --nginx -d $DOMAIN"
    fi
fi

# -------------------------------------------------------- health + state --
say "Waiting for the backend to answer"
for _ in $(seq 1 60); do
    if curl -fsS -m 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then break; fi
    sleep 2
done
curl -fsS -m 5 http://127.0.0.1:8000/health >/dev/null 2>&1 \
    || die "backend did not become healthy — see: journalctl -u chattermate-backend -n 100"
curl -fsS -m 5 "$PUBLIC_URL/" -o /dev/null \
    || warn "frontend did not answer at $PUBLIC_URL/ yet — check nginx: nginx -t && systemctl status nginx"
ok "backend healthy at http://127.0.0.1:8000/health"

{
    echo "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "repo_url=$REPO_URL"
    echo "branch=$BRANCH"
    echo "built_commit=$HEAD_COMMIT"
    echo "public_url=$PUBLIC_URL"
    echo "install_dir=$INSTALL_DIR"
} > "$STATE_FILE"
chown "$APP_USER:$APP_USER" "$STATE_FILE"

# ------------------------------------------------------------------ done ---
cat <<EOF | tee -a "$LOG_FILE"

======================================================================
  ChatterMate is installed and running.
----------------------------------------------------------------------
  App:       $PUBLIC_URL/
  Setup:     $PUBLIC_URL/setup        <- open this first
  API health: $PUBLIC_URL/health

  First step: open $PUBLIC_URL/setup and create your admin account.
  The first account created there owns the workspace — there is no
  default password to change.

  Add your AI provider key afterwards in the dashboard
  (Settings -> AI Setup), or the AI chat will not answer.

  Manage it:
    systemctl status chattermate-backend     backend service
    journalctl -u chattermate-backend -f     backend logs
    sudo bash $INSTALL_DIR/update.sh         pull + rebuild + restart
    nginx -t && systemctl reload nginx       after editing the nginx site

  Secrets live in $INSTALL_DIR/backend/.env
  (database password, JWT + encryption keys). Back that file up
  separately from the database — losing ENCRYPTION_KEY makes all
  stored credentials and chat history unreadable.
  Full install log: $LOG_FILE
======================================================================
EOF
