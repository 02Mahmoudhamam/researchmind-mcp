#!/usr/bin/env bash
# Repository hygiene checks. Run locally before pushing; CI runs the same file.
set -uo pipefail
fail=0
say() { printf '%s\n' "$*"; }
ok()  { printf '  ✅ %s\n' "$*"; }
bad() { printf '  ❌ %s\n' "$*"; fail=1; }

say "== 1. No real .env files tracked =="
if git ls-files | grep -E '^(.*/)?\.env($|\.)' | grep -v '\.env\.example$'; then
  bad "a real .env file is tracked"
else ok "only .env.example files are tracked"; fi

say "== 2. No file larger than 5 MB =="
# GitHub's hard limit is 100 MB. 5 MB is a deliberately tighter tripwire: this
# tree contained a 1.1 GB VS Code IntelliSense database.
big=$(git ls-files -z | xargs -0 -r du -b 2>/dev/null | awk '$1 > 5242880 {print $1" "$2}')
if [ -n "$big" ]; then bad "oversized tracked files:"; printf '     %s\n' "$big"
else ok "no tracked file exceeds 5 MB"; fi

say "== 3. No build artefacts or caches tracked =="
if git ls-files | grep -E '(__pycache__|\.pyc$|\.pytest_cache/|\.mypy_cache/|\.ruff_cache/|node_modules/|^\.next/|/\.next/|\.vscode/)'; then
  bad "generated artefacts are tracked"
else ok "no caches, bytecode, node_modules, .next or .vscode tracked"; fi

say "== 4. Credential patterns in tracked files =="
# Format-based detection only. This is a tripwire, not a security control.
if git ls-files -z | xargs -0 -r grep -IlE \
   'sk-ant-[A-Za-z0-9]|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,}' 2>/dev/null; then
  bad "possible credential found in the files listed above"
else ok "no credential patterns in tracked files"; fi

say "== 5. .gitignore and .dockerignore present =="
[ -f .gitignore ]    && ok ".gitignore present"    || bad ".gitignore missing"
[ -f .dockerignore ] && ok ".dockerignore present" || bad ".dockerignore missing"

say "== 6. Internal markdown links resolve =="
broken=0
for f in $(git ls-files '*.md'); do
  for l in $(grep -oE '\]\((\.{0,2}/)?[A-Za-z0-9_./-]+\.md[^)]*\)' "$f" 2>/dev/null \
             | sed -E 's/^\]\(//; s/\)$//; s/#.*//'); do
    case "$l" in /*) continue;; esac
    [ -e "$(dirname "$f")/$l" ] || { printf '     BROKEN %s -> %s\n' "$f" "$l"; broken=1; }
  done
done
[ "$broken" -eq 0 ] && ok "all internal markdown links resolve" || bad "broken markdown links"

echo
[ "$fail" -eq 0 ] && { echo "Repository hygiene: PASS"; exit 0; } \
                  || { echo "Repository hygiene: FAIL"; exit 1; }
