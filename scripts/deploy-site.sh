#!/usr/bin/env bash
# Serve one of this checkout's static sites with the nginx already on this machine: lumiverb.io's
# front page (site/, the default) or docs.lumiverb.io's guide (--folder docs-site, built from
# docs/guide by scripts/build_docs.py and kept built in the repository).
#
# Made for the demo server (docs/demo-server.md), beside its web app: a site of its own in nginx,
# whose files are the checkout's folder as they are (no build here), so each update (update.sh)
# brings the site's changes with Lumiverb's. A certificate from Let's Encrypt for the names that
# point here, as deploy-web.sh gets one.
#
# Usage:
#   sudo bash /opt/lumiverb/scripts/deploy-site.sh --domain lumiverb.io --domain www.lumiverb.io
#   sudo bash /opt/lumiverb/scripts/deploy-site.sh --folder docs-site --domain docs.lumiverb.io
#
# Safe to run again: a name whose DNS record was added since gets its certificate then.
#
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

step()  { echo -e "\n${BOLD}=== $1 ===${NC}"; }
ok()    { echo -e "${GREEN}  ✓${NC} $1"; }
warn()  { echo -e "${YELLOW}  ⚠${NC} $1"; }
fail()  { echo -e "${RED}  ✗ $1${NC}" >&2; exit 1; }
# set -e stops at the first failed command; say which, so a stop is never silent.
trap 'echo -e "${RED}  ✗ Stopped at line ${LINENO}: ${BASH_COMMAND}${NC}" >&2' ERR

DOMAINS=()
FOLDER="site"
CERTBOT_EMAIL=""
SKIP_CERTBOT=false
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)       DOMAINS+=("${2:?Missing value for --domain}"); shift 2 ;;
    --folder)       FOLDER="${2:?Missing value for --folder}"; shift 2 ;;
    --email)        CERTBOT_EMAIL="${2:?Missing value for --email}"; shift 2 ;;
    --skip-certbot) SKIP_CERTBOT=true; shift ;;
    --dry-run)      DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 [--folder site|docs-site] --domain <name> [--domain <name> ...] [--email <certbot-email>] [--skip-certbot] [--dry-run]"
      echo "  --folder      The checkout's folder to serve: site (lumiverb.io, the default) or docs-site (docs.lumiverb.io)"
      echo "  --domain      A name the site answers to; once for each (lumiverb.io, www.lumiverb.io)"
      echo "  --dry-run     Print the nginx site it would write, and change nothing"
      exit 0
      ;;
    *) fail "Unknown option: $1" ;;
  esac
done

[[ ${#DOMAINS[@]} -gt 0 ]] || fail "Required: --domain <name>, once for each name (e.g. lumiverb.io and www.lumiverb.io)"
for d in "${DOMAINS[@]}"; do
  # Each goes into nginx's config as it's typed: a name, nothing else.
  [[ "$d" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
    || fail "Not a domain name: ${d}"
  [[ "$d" != *"example.com" ]] || fail "Replace example.com with your actual domain"
done

# Its name goes into paths and nginx's config: a folder's name, nothing else.
[[ "$FOLDER" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || fail "Not a folder of this checkout: ${FOLDER}"
APP_DIR="${LUMIVERB_APP_DIR:-/opt/lumiverb}"
SITE_DIR="${APP_DIR}/${FOLDER}"
SITE_CONF="${LUMIVERB_SITE_CONF:-/etc/nginx/sites-available/lumiverb-${FOLDER}}"
SITE_LINK="${LUMIVERB_SITE_LINK:-/etc/nginx/sites-enabled/lumiverb-${FOLDER}}"
[[ -f "${SITE_DIR}/index.html" ]] || fail "No site in ${SITE_DIR}: update the checkout first (scripts/update.sh)"

# Its pages load only their own files (each page's own Content-Security-Policy says the same:
# tests/test_deploy_site.py).
CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"

# Only the site/ folder is served (never the checkout around it), each file as it is, and nothing else: a
# missing file is a 404, not the page.
site_config() {
  cat <<NGINX
server {
    listen 80;
    server_name ${DOMAINS[*]};
    access_log /var/log/nginx/access.log lumiverb_host;

    root ${SITE_DIR};
    index index.html;
    charset utf-8;

    add_header X-Content-Type-Options nosniff always;
    add_header Content-Security-Policy "${CSP}" always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;
    server_tokens off;

    location / {
        try_files \$uri \$uri/ =404;
    }
}
NGINX
}

if [[ "$DRY_RUN" == "true" ]]; then
  site_config
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || fail "This script must be run as root (try: sudo bash ...)"
command -v nginx >/dev/null 2>&1 || fail "nginx isn't installed here: install Lumiverb's web app first (deploy-web.sh)"

# ---------------------------------------------------------------------------
# Whether each name points here: deploy-web.sh's check, held to the same text by tests/test_deploy_site.py.
# ---------------------------------------------------------------------------
# A v4 address that isn't the internet's (a router's network, CGNAT and Tailscale, link-local, loopback).
private_v4() {
  [[ "$1" =~ ^(10|127)\. || "$1" =~ ^192\.168\. || "$1" =~ ^169\.254\. \
    || "$1" =~ ^172\.(1[6-9]|2[0-9]|3[01])\. || "$1" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]
}

# Says what's wrong and returns 1 when the domain doesn't resolve, or resolves somewhere this machine's public
# address isn't. Behind a router (no public address of that family here) it can't tell, and lets it through.
domain_points_here() {
  local domain="$1" addr resolved mine public_v4="" public_v6="" elsewhere=""
  resolved="$(getent ahosts "$domain" 2>/dev/null | awk '{ print $1 }' | sort -u || true)"
  mine="$(ip -o addr show scope global 2>/dev/null | awk '{ sub(/\/.*/, "", $4); print $4 }' || true)"
  for addr in $mine; do
    if [[ "$addr" == *:* ]]; then
      public_v6="${public_v6:-$addr}"
    elif ! private_v4 "$addr"; then
      public_v4="${public_v4:-$addr}"
    fi
  done
  if [[ -z "$resolved" ]]; then
    warn "${domain} doesn't resolve yet: add an A record for it pointing at ${public_v4:-the address the internet reaches this machine at}"
    return 1
  fi
  for addr in $resolved; do
    [[ "$addr" == 127.* || "$addr" == "::1" ]] && return 0  # /etc/hosts names this machine: DNS isn't asked
    grep -qxF "$addr" <<<"$mine" && continue
    if [[ "$addr" == *:* && -n "$public_v6" ]]; then
      elsewhere+=" Its AAAA record points at ${addr}: change it to ${public_v6}, or remove it."
    elif [[ "$addr" != *:* && -n "$public_v4" ]]; then
      elsewhere+=" Its A record points at ${addr}: change it to ${public_v4}."
    fi
  done
  if [[ -n "$elsewhere" ]]; then
    warn "${domain} doesn't point at this machine.${elsewhere}"
    return 1
  fi
}

# HSTS on HTTPS server blocks only: beside each nosniff header in a block
# that listens on 443 (certbot's port-80 redirect block has none).
add_hsts() {
  local site="$1"
  grep -q "Strict-Transport-Security" "$site" && return 0
  awk '
    function flush(   i, pad) {
      for (i = 1; i <= n; i++) {
        print buf[i]
        if (tls && buf[i] ~ /add_header X-Content-Type-Options/) {
          pad = buf[i]; sub(/add_header.*/, "", pad)
          print pad "add_header Strict-Transport-Security \"max-age=31536000\" always;"
        }
      }
      n = 0; tls = 0
    }
    /^server[[:space:]]*\{/ { flush(); inblock = 1 }
    { if (inblock) { buf[++n] = $0; if ($1 == "listen" && $2 ~ /(^|:)443;?$/) tls = 1 } else print }
    /^\}/ && inblock { flush(); inblock = 0 }
    END { flush() }
  ' "$site" > "${site}.new"
  mv "${site}.new" "$site"
}

# ---------------------------------------------------------------------------
# 1. The nginx site
# ---------------------------------------------------------------------------
step "Configuring nginx"

# Its access log's lines name the site (lumiverb_host, for lumiverb-server site-visits): the format first.
python3 -I "${APP_DIR}/src/server/lifecycle/site_update.py" --log-format "${LUMIVERB_NGINX_CONF_D:-/etc/nginx/conf.d}/lumiverb-log.conf"
site_config > "$SITE_CONF"
ln -sf "$SITE_CONF" "$SITE_LINK"
# A site nginx refuses would stop every later reload (the web app's too): it goes again at once.
if ! nginx -t; then
  rm -f "$SITE_LINK"
  fail "nginx refused the site (above): removed it again, nothing else changed"
fi
systemctl reload nginx
ok "nginx serves ${SITE_DIR} as ${DOMAINS[*]}"

# ---------------------------------------------------------------------------
# 2. TLS certificate, for the names that point here
# ---------------------------------------------------------------------------
TLS_ACTIVE=false
if [[ "$SKIP_CERTBOT" == "true" ]]; then
  warn "Skipping certbot (--skip-certbot)"
else
  step "Obtaining TLS certificate"
  command -v certbot >/dev/null 2>&1 || apt-get install -y -qq certbot python3-certbot-nginx
  HERE=()
  for d in "${DOMAINS[@]}"; do
    if domain_points_here "$d"; then HERE+=("$d"); fi
  done
  if [[ ${#HERE[@]} -eq 0 ]]; then
    warn "Not asked for yet: the site serves HTTP only until its names point here; then run this again"
  else
    [[ ${#HERE[@]} -eq ${#DOMAINS[@]} ]] || warn "Asked for ${HERE[*]} only: run this again once the others point here"
    certbot_args=(--nginx --non-interactive --agree-tos --redirect --expand)
    for d in "${HERE[@]}"; do certbot_args+=(-d "$d"); done
    if [[ -n "$CERTBOT_EMAIL" ]]; then
      certbot_args+=(--email "$CERTBOT_EMAIL")
    else
      certbot_args+=(--register-unsafely-without-email)
    fi
    if certbot "${certbot_args[@]}"; then
      ok "TLS certificate for ${HERE[*]}"
      TLS_ACTIVE=true
    else
      warn "Certbot failed: the site serves HTTP only until port 80 is reachable for those names; then run this again"
    fi
  fi
fi

if [[ "$TLS_ACTIVE" == "true" ]]; then
  add_hsts "$SITE_CONF"
  # nginx's version unsaid on certbot's redirect too (update-web.sh gives an existing site the same).
  python3 -I "${APP_DIR}/src/server/lifecycle/site_update.py" --static "$SITE_CONF"
  nginx -t
  systemctl reload nginx
  ok "HSTS on"
fi

# ---------------------------------------------------------------------------
# 3. Verify
# ---------------------------------------------------------------------------
step "Verifying"
if curl -sf -H "Host: ${DOMAINS[0]}" http://127.0.0.1/ 2>/dev/null | grep -q "<title>Lumiverb"; then
  ok "The page answers for ${DOMAINS[0]}"
elif [[ "$TLS_ACTIVE" == "true" ]] && curl -sf --resolve "${DOMAINS[0]}:443:127.0.0.1" "https://${DOMAINS[0]}/" 2>/dev/null \
    | grep -q "<title>Lumiverb"; then
  ok "The page answers for ${DOMAINS[0]} over HTTPS"
else
  warn "The page didn't answer for ${DOMAINS[0]} here: see nginx's error log (/var/log/nginx/error.log)"
fi

echo ""
echo -e "${GREEN}${BOLD}${DOMAINS[*]} served from ${SITE_DIR}${NC}"
echo ""
echo "It changes when this checkout does: each update (scripts/update.sh) brings the site's changes with Lumiverb's."
