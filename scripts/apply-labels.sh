#!/usr/bin/env bash
# Apply the label taxonomy in .github/labels.yml to the GitHub repository.
# Requires an authenticated gh CLI:  gh auth login
set -euo pipefail
cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo "gh CLI not installed"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated — run: gh auth login"; exit 1; }

python3 - <<'PY' | while IFS=$'\t' read -r name color desc; do
import re, pathlib
text = pathlib.Path(".github/labels.yml").read_text()
blocks = re.findall(
    r'- name: "([^"]+)"\s*\n\s*color: "([^"]+)"\s*\n\s*description: (.+)',
    text,
)
for name, color, desc in blocks:
    print(f"{name}\t{color}\t{desc.strip()}")
PY
  echo "  label: $name"
  gh label create "$name" --color "$color" --description "$desc" --force
done
echo "Labels applied."
