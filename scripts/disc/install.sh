#!/bin/bash
# ==============================================================================
# Installs the `disc` CLI (devtron migration info tool) for the current user.
#
# Usage:
#   ./install.sh
#
# What it does:
#   - Copies bin/disc + libexec/* from this directory into ~/opt/disc
#   - Symlinks ~/.local/bin/disc -> ~/opt/disc/bin/disc
#   - Adds ~/.local/bin to PATH in your shell rc file (if not already there)
#   - Prompts for the path to your local helm-app-non-prod clone and sets
#     DISC_HELM_CHART_REPO in your shell rc file (needed for `disc eks`)
#   - Checks for aws-cli, jq, python3 + pyyaml (needed for `disc ecs` / `disc eks`)
#
# Safe to re-run: it overwrites the installed copies and skips lines that
# are already present in your shell rc file.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISC_HOME="$HOME/opt/disc"
LOCAL_BIN="$HOME/.local/bin"

echo "Installing disc CLI into $DISC_HOME"

mkdir -p "$DISC_HOME/bin" "$DISC_HOME/libexec" "$LOCAL_BIN"

cp "$SCRIPT_DIR/bin/disc" "$DISC_HOME/bin/disc"
cp "$SCRIPT_DIR/libexec/disc-ecs.sh" "$DISC_HOME/libexec/disc-ecs.sh"
cp "$SCRIPT_DIR/libexec/disc-eks.sh" "$DISC_HOME/libexec/disc-eks.sh"
cp "$SCRIPT_DIR/libexec/disc-eks-flatten.py" "$DISC_HOME/libexec/disc-eks-flatten.py"

chmod +x "$DISC_HOME/bin/disc" \
         "$DISC_HOME/libexec/disc-ecs.sh" \
         "$DISC_HOME/libexec/disc-eks.sh" \
         "$DISC_HOME/libexec/disc-eks-flatten.py"

ln -sf "$DISC_HOME/bin/disc" "$LOCAL_BIN/disc"
echo "Linked $LOCAL_BIN/disc -> $DISC_HOME/bin/disc"

# --- pick a shell rc file ---
case "${SHELL:-}" in
    */zsh) RC_FILE="$HOME/.zshrc" ;;
    */bash) RC_FILE="$HOME/.bashrc" ;;
    *) RC_FILE="$HOME/.zshrc" ;;
esac
touch "$RC_FILE"
echo "Using shell rc file: $RC_FILE"

# --- PATH ---
if ! grep -qF 'export PATH="$HOME/.local/bin:$PATH"' "$RC_FILE"; then
    {
        echo ""
        echo "# added by devtron-automation/scripts/disc/install.sh"
        echo 'export PATH="$HOME/.local/bin:$PATH"'
    } >> "$RC_FILE"
    echo "Added ~/.local/bin to PATH in $RC_FILE"
else
    echo "PATH already configured in $RC_FILE"
fi

# --- DISC_HELM_CHART_REPO ---
if grep -q '^export DISC_HELM_CHART_REPO=' "$RC_FILE"; then
    echo "DISC_HELM_CHART_REPO already set in $RC_FILE, leaving it as-is"
else
    read -r -p "Path to your local clone of helm-app-non-prod (needed for 'disc eks', leave blank to set later): " CHART_REPO_PATH
    if [[ -n "$CHART_REPO_PATH" ]]; then
        {
            echo ""
            echo "# added by devtron-automation/scripts/disc/install.sh"
            echo "export DISC_HELM_CHART_REPO=\"$CHART_REPO_PATH\""
        } >> "$RC_FILE"
        echo "Added DISC_HELM_CHART_REPO to $RC_FILE"
        if [[ ! -d "$CHART_REPO_PATH" ]]; then
            echo "Note: that path doesn't exist yet. Clone it first, e.g.:"
            echo "  git clone git@bitbucket.org:fastbanking/helm-app-non-prod.git \"$CHART_REPO_PATH\""
        fi
    else
        echo "Skipped. Set it later by adding this to $RC_FILE:"
        echo '  export DISC_HELM_CHART_REPO="/path/to/helm-app-non-prod"'
    fi
fi

# --- dependency checks ---
echo ""
echo "Checking dependencies..."

for cmd in aws jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "  warning: '$cmd' not found on PATH - required for 'disc ecs'"
    fi
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "  warning: python3 not found on PATH - required for 'disc eks'"
elif ! python3 -c "import yaml" >/dev/null 2>&1; then
    echo "  warning: python3 module 'pyyaml' not found - required for 'disc eks'"
    echo "  install it with: pip3 install --user pyyaml"
fi

echo ""
echo "Done. Open a new terminal (or run: source $RC_FILE), then try:"
echo "  disc ecs <cluster-name> <service-name> [region]"
echo "  disc eks <org> <service-name>"
