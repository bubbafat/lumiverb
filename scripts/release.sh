#!/usr/bin/env bash
# Release Lumiverb: tag main, make the GitHub release, and open the Homebrew
# tap's PR moving the formula to it (merge that PR to ship it: `brew
# upgrade lumiverb` then finds the new version).
#
#   scripts/release.sh v0.1.1 [--dry-run]
#
# Tags origin/main as it is now, whatever is checked out here. --dry-run
# says what it would do and changes nothing. Needs git and gh, signed in.
# Run again after a failure: a version already tagged and released goes
# straight to the tap's PR, unless the tap has it already.
set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
step() { echo -e "\n${BOLD}=== $1 ===${NC}"; }
ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
fail() { echo -e "${RED}  ✗ $1${NC}" >&2; exit 1; }

REPO="bubbafat/lumiverb"
TAP="bubbafat/homebrew-lumiverb"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VERSION="${1:-}"
DRY_RUN=false
[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=true
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Say the version: scripts/release.sh vX.Y.Z [--dry-run]"
command -v gh >/dev/null || fail "gh isn't installed"
[[ "$(git remote get-url origin 2>/dev/null)" == *"${REPO}"* ]] || fail "Run it in a Lumiverb checkout (origin: ${REPO})"

step "What it releases"
git fetch -q origin main
COMMIT="$(git rev-parse origin/main)"
ARCHIVE="https://github.com/${REPO}/archive/refs/tags/${VERSION}.tar.gz"
TAGGED=false
if git ls-remote --exit-code --tags origin "refs/tags/${VERSION}" >/dev/null; then
  TAGGED=true
  if gh api "repos/${TAP}/contents/Formula/lumiverb.rb" --jq .content | base64 --decode | grep -qF "$ARCHIVE"; then
    fail "${VERSION} is released already, and the tap has it"
  fi
  gh release view "$VERSION" --repo "$REPO" >/dev/null 2>&1 || fail "${VERSION} is tagged but has no GitHub release: make it by hand, then run this again"
  ok "${VERSION} is tagged and released; the tap's PR is still to make"
else
  ok "${VERSION} = main at $(git log -1 --format='%h %s' "$COMMIT")"
fi
if $DRY_RUN; then
  $TAGGED || ok "Would tag it and make the GitHub release"
  ok "Would open a tap PR with ${ARCHIVE} and its sha256"
  exit 0
fi

if ! $TAGGED; then
  step "Tag and release"
  git tag -a "$VERSION" "$COMMIT" -m "Lumiverb ${VERSION}"
  git push -q origin "refs/tags/${VERSION}"
  gh release create "$VERSION" --repo "$REPO" --title "Lumiverb ${VERSION}" --generate-notes >/dev/null
  ok "Released ${VERSION}"
fi

step "The tap's PR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# GitHub makes the archive on first request: the sha256 is of what Homebrew will download.
curl -fsSL "$ARCHIVE" -o "${WORK}/release.tar.gz"
if command -v sha256sum >/dev/null; then HASH=(sha256sum); else HASH=(shasum -a 256); fi
SHA256="$("${HASH[@]}" "${WORK}/release.tar.gz" | cut -d' ' -f1)"
[[ "$SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "Couldn't hash the release archive"
gh repo clone "$TAP" "${WORK}/tap" -- -q
python3 "${HERE}/bump_formula.py" "${WORK}/tap/Formula/lumiverb.rb" "$ARCHIVE" "$SHA256"
git -C "${WORK}/tap" switch -q -c "lumiverb-${VERSION}"
git -C "${WORK}/tap" commit -q -am "lumiverb ${VERSION}"
git -C "${WORK}/tap" push -q origin "lumiverb-${VERSION}"
URL="$(gh pr create --repo "$TAP" --base main --head "lumiverb-${VERSION}" --title "lumiverb ${VERSION}" \
  --body "The formula moves to Lumiverb ${VERSION} (${ARCHIVE}, sha256 ${SHA256}). Merge it to ship: \`brew upgrade lumiverb\` then finds it, and the service restarts into it by itself.")"
ok "Tap PR: ${URL}"
