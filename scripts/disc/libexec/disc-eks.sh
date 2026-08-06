#!/bin/bash
# ==============================================================================
# Prints every field found in a service's Helm values file from the
# helm-app-non-prod chart repo.
#
# Usage:
#   disc eks <org> <service-name>
#
# Example:
#   disc eks ring acquisition-consumer-go
#   disc eks kissh loan-service
#
# Prints plain text to stdout. No output file.
# ==============================================================================

set -euo pipefail

ORG_INPUT=${1:-}
SERVICE=${2:-}

if [[ -z "$ORG_INPUT" || -z "$SERVICE" ]]; then
    echo "Usage: disc eks <org> <service-name>"
    exit 1
fi

if [[ -z "${DISC_HELM_CHART_REPO:-}" ]]; then
    echo "DISC_HELM_CHART_REPO is not set."
    echo "Add this to your ~/.zshrc and open a new shell:"
    echo '  export DISC_HELM_CHART_REPO="/path/to/helm-app-non-prod"'
    exit 1
fi

if [[ ! -d "$DISC_HELM_CHART_REPO" ]]; then
    echo "DISC_HELM_CHART_REPO is set to $DISC_HELM_CHART_REPO but that directory does not exist."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLATTEN_PY="$SCRIPT_DIR/disc-eks-flatten.py"

ORG=""
LC_ORG_INPUT=$(echo "$ORG_INPUT" | tr '[:upper:]' '[:lower:]')

for d in "$DISC_HELM_CHART_REPO"/*/; do
    BASE=$(basename "$d")
    LC_BASE=$(echo "$BASE" | tr '[:upper:]' '[:lower:]')
    if [[ "$LC_BASE" == "$LC_ORG_INPUT"* || "$LC_ORG_INPUT" == "$LC_BASE"* ]]; then
        ORG="$BASE"
        break
    fi
done

if [[ -z "$ORG" ]]; then
    echo "No org matching '$ORG_INPUT' found under $DISC_HELM_CHART_REPO"
    exit 1
fi

if [[ ! -d "$DISC_HELM_CHART_REPO/$ORG/dev" ]]; then
    echo "No dev/ folder found under $DISC_HELM_CHART_REPO/$ORG"
    exit 1
fi

MATCHES=$(find "$DISC_HELM_CHART_REPO/$ORG/dev" -mindepth 2 -maxdepth 2 -type d -iname "$SERVICE")

if [[ -z "$MATCHES" ]]; then
    echo "No service folder named '$SERVICE' found under $ORG/dev/*"
    exit 1
fi

echo "$MATCHES" | while IFS= read -r DIR; do

    FILES=$(find "$DIR" -maxdepth 1 -type f \( -iname "*.yaml" -o -iname "*.yml" \))

    if [[ -z "$FILES" ]]; then
        echo "no yaml files found in $DIR"
        continue
    fi

    echo "$FILES" | while IFS= read -r FILE; do
        RELATIVE=${FILE#"$DISC_HELM_CHART_REPO"/}
        echo "===== $RELATIVE ====="
        python3 "$FLATTEN_PY" "$FILE"
        echo ""
    done

done
