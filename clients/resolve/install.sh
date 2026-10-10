#!/usr/bin/env bash
# Installs the Lumiverb Workflow Integration into DaVinci Resolve Studio (macOS).
# Copies plugin/ plus Blackmagic's WorkflowIntegration.node (shipped with
# Resolve, not kept in this repo) into Resolve's plugin folder. Run it again
# after changing the plugin, then restart Resolve.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
support="/Library/Application Support/Blackmagic Design/DaVinci Resolve"
dest="$support/Workflow Integration Plugins/Lumiverb"
module="$support/Developer/Workflow Integrations/Examples/SamplePlugin/WorkflowIntegration.node"

if [ ! -f "$module" ]; then
    echo "Can't find WorkflowIntegration.node. Is DaVinci Resolve Studio installed?" >&2
    exit 1
fi

mkdir -p "$dest"
cp "$here"/plugin/* "$dest/"
cp "$module" "$dest/"
echo "Installed in $dest"
echo "Restart Resolve, then open Workspace > Workflow Integrations > Lumiverb."
