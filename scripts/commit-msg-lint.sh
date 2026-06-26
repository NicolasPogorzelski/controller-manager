#!/usr/bin/env bash
# Lint a commit message against the repository's Conventional Commits policy.
#
# Usage:
#   ./scripts/commit-msg-lint.sh "feat(manager): add hotplug support"
#   ./scripts/commit-msg-lint.sh path/to/commit-msg-file
#
# As a git hook (.git/hooks/commit-msg):
#   #!/usr/bin/env bash
#   exec "$(git rev-parse --show-toplevel)/scripts/commit-msg-lint.sh" "$1"
set -euo pipefail

TYPES="feat|fix|docs|refactor|chore|test|ci"
SCOPES="manager|gate|service|install|docs|ci|repo"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 \"<commit message>\" | <commit-msg-file>" >&2
    exit 2
fi

# Accept either a literal message or a path to a message file.
if [[ -f "$1" ]]; then
    SUBJECT="$(head -n1 "$1")"
    BODY="$(cat "$1")"
else
    SUBJECT="$(printf '%s' "$1" | head -n1)"
    BODY="$1"
fi

FAIL=0

# Subject format: <type>(<scope>): <description>
if [[ ! "${SUBJECT}" =~ ^(${TYPES})\((${SCOPES})\):\ .+ ]]; then
    echo "ERROR: subject must match '<type>(<scope>): <description>'" >&2
    echo "       types:  ${TYPES//|/, }" >&2
    echo "       scopes: ${SCOPES//|/, }" >&2
    echo "       got:    ${SUBJECT}" >&2
    FAIL=1
fi

# No AI / assistant attribution anywhere in the message.
if printf '%s' "${BODY}" | grep -qiE 'co-authored-by:.*(claude|anthropic|gpt|copilot)|generated with|ai assistant'; then
    echo "ERROR: commit messages must not contain AI/assistant attribution" >&2
    FAIL=1
fi

if [[ "${FAIL}" -eq 0 ]]; then
    echo "OK: ${SUBJECT}"
fi
exit "${FAIL}"
