#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ERRORS=0
ERROR_LOG="$(mktemp)"
trap 'rm -f "${ERROR_LOG}"' EXIT

echo "=== Repo Validation ==="
echo "Repo root: ${REPO_ROOT}"
echo ""

# =============================================================================
# Check 1: empty markdown files
# =============================================================================
echo "Check 1: empty markdown files"

while read -r file; do
    echo "  Empty: ${file}"
    ERRORS=$((ERRORS + 1))
done < <(find "${REPO_ROOT}" -not -path "*/.git/*" -name "*.md" -empty)

# =============================================================================
# Check 2: broken internal markdown links
# =============================================================================
echo "Check 2: broken internal links"

while read -r mdfile; do
    dir="$(dirname "${mdfile}")"
    { grep -oP '\]\(\K[^)]+' "${mdfile}" || true; } | while read -r link; do
        [[ "${link}" =~ ^https?:// ]] && continue
        [[ "${link}" =~ ^# ]] && continue
        link="${link%%#*}"
        [[ -z "${link}" ]] && continue
        if [[ ! -e "${dir}/${link}" ]]; then
            echo "  Broken: ${mdfile} -> ${link}"
            echo "x" >> "${ERROR_LOG}"
        fi
    done
done < <(find "${REPO_ROOT}" -not -path "*/.git/*" -name "*.md" -type f)

# =============================================================================
# Check 3: committed secrets (.env files other than examples)
# =============================================================================
echo "Check 3: committed secret files"

while read -r file; do
    echo "  Found: ${file}"
    echo "x" >> "${ERROR_LOG}"
done < <(find "${REPO_ROOT}" -not -path "*/.git/*" \( -name ".env" -o -name ".env.*" \) -not -name ".env.example")

# =============================================================================
# Check 4: runbook contract sections
# =============================================================================
# Contract from runbooks/README.md: Preconditions, Steps, Verification,
# Failure modes (Rollback where applicable).
echo "Check 4: runbook contract sections"

RUNBOOK_SECTIONS=("Precondition" "Verification" "Failure")

if [[ -d "${REPO_ROOT}/runbooks" ]]; then
    while read -r file; do
        [[ "$(basename "${file}")" == "README.md" ]] && continue
        for section in "${RUNBOOK_SECTIONS[@]}"; do
            if ! grep -qi "${section}" "${file}"; then
                echo "  ${file}: missing '${section}' section"
                echo "x" >> "${ERROR_LOG}"
            fi
        done
    done < <(find "${REPO_ROOT}/runbooks" -name "*.md" -type f)
fi

# =============================================================================
# Check 5: Python daemon syntax
# =============================================================================
echo "Check 5: Python syntax"

if ! python3 -m py_compile "${REPO_ROOT}/controller-manager.py" 2>/dev/null; then
    echo "  controller-manager.py: py_compile failed"
    echo "x" >> "${ERROR_LOG}"
fi

# =============================================================================
# Check 6: shell scripts (shellcheck, if available)
# =============================================================================
echo "Check 6: shell scripts (shellcheck)"

if command -v shellcheck &>/dev/null; then
    SHELL_FILES=()
    while IFS= read -r f; do SHELL_FILES+=("$f"); done < \
        <(find "${REPO_ROOT}" -not -path "*/.git/*" -name "*.sh" -type f | sort)
    SHELL_FILES+=("${REPO_ROOT}/controller-hidraw-gate")
    SHELL_FILES+=("${REPO_ROOT}/controller-led")

    for f in "${SHELL_FILES[@]}"; do
        if ! shellcheck "$f" 2>/dev/null; then
            echo "  shellcheck failed: $(basename "$f")"
            echo "x" >> "${ERROR_LOG}"
        fi
    done
else
    echo "  (shellcheck not found - skipped; CI enforces it)"
fi

# =============================================================================
# Check 7: unit tests (reconcile logic, dbusmenu id model, ...)
# =============================================================================
echo "Check 7: unit tests"

while read -r testfile; do
    if ! python3 "${testfile}" >/dev/null 2>&1; then
        echo "  ${testfile#"${REPO_ROOT}"/}: FAILED"
        echo "x" >> "${ERROR_LOG}"
    fi
done < <(find "${REPO_ROOT}/tests" -name "test_*.py" -type f 2>/dev/null | sort)

# =============================================================================
# Check 8: plain-ASCII punctuation
# =============================================================================
# Documentation, docstrings and comments here are read in terminals, greps and
# diffs, where typographic punctuation renders inconsistently across fonts and is
# awkward to search for, since several look-alike codepoints exist. The code is
# ASCII anyway, so the prose follows the same rule rather than keeping a second
# convention.
#
# Exception: box-drawing characters (U+2500-U+257F), which are the conventional
# tool for the architecture diagrams.
echo "Check 8: plain-ASCII punctuation"

while read -r file; do
    rel="${file#"${REPO_ROOT}"/}"
    git -C "${REPO_ROOT}" check-ignore -q "${rel}" 2>/dev/null && continue
    { grep -noP '(?![\x{2500}-\x{257F}])[^\x00-\x7F]' "${file}" || true; } | while read -r match; do
        echo "  Non-ASCII punctuation: ${rel}:${match}"
        echo "x" >> "${ERROR_LOG}"
    done
done < <(find "${REPO_ROOT}" -not -path "*/.git/*" -type f \
              \( -name "*.md" -o -name "*.py" -o -name "*.sh" -o -name "*.yml" \
                 -o -name "*.rules" -o -name "*.sudoers" -o -name "*.c" \))

# =============================================================================
# Result
# =============================================================================
ERRORS=$((ERRORS + $(wc -l < "${ERROR_LOG}")))

echo ""
if [[ "${ERRORS}" -eq 0 ]]; then
    echo "PASS: no problems found."
    exit 0
else
    echo "FAIL: ${ERRORS} problem(s) found."
    exit 1
fi
