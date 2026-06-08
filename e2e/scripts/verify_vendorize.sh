#!/usr/bin/env bash
# Commit-scope guard for Stage 0: fail if the HEAD vendorize commit touched any path
# outside the allowed vendored set. Run inside the Test-Instances checkout.
#
# Usage: verify_vendorize.sh [<commit-ish>]   (default HEAD)
set -euo pipefail

commit="${1:-HEAD}"

# Allowed top-level prefixes a vendorize commit may touch.
allowed_re='^(\.github/|scripts/|cloud-config$|issue-commands\.md$|course-issue-commands\.md$|workshop-issue-commands\.md$)'

# Read changed files into an array WITHOUT mapfile (macOS ships bash 3.2, no mapfile).
changed=()
while IFS= read -r line; do
  [[ -n "$line" ]] && changed+=("$line")
done < <(git show --no-color --name-only --pretty=format: "$commit")

if [[ ${#changed[@]} -eq 0 ]]; then
  echo "verify_vendorize: no files changed in $commit (no-op vendorize) — OK."
  exit 0
fi

bad=()
for f in "${changed[@]}"; do
  if [[ ! "$f" =~ $allowed_re ]]; then
    bad+=("$f")
  fi
done

if [[ ${#bad[@]} -gt 0 ]]; then
  echo "::error ::verify_vendorize: commit $commit touched non-vendored path(s):" >&2
  printf '  %s\n' "${bad[@]}" >&2
  echo "Aborting — a vendorize commit must only touch .github/**, scripts/**, cloud-config, *-commands.md." >&2
  exit 1
fi

echo "verify_vendorize: $commit touches only vendored paths (${#changed[@]} file(s)) — OK."
