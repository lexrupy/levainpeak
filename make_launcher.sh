#!/usr/bin/env bash
# Installs the LevainPeak launcher (.desktop) for the current user,
# pointing at this checkout and its .venv.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="$project_dir/packaging/levainpeak.desktop.in"
target_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
target="$target_dir/levainpeak.desktop"

if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
    echo "Missing $project_dir/.venv. Create it first (see README)." >&2
    exit 1
fi

mkdir -p "$target_dir"
# '|' as sed delimiter; escape '&' and '|' that may appear in the path.
escaped="$(printf '%s' "$project_dir" | sed -e 's/[&|\\]/\\&/g')"
sed "s|@PROJECT_DIR@|$escaped|g" "$template" > "$target"
chmod 644 "$target"

command -v desktop-file-validate >/dev/null && desktop-file-validate "$target"
command -v update-desktop-database >/dev/null && update-desktop-database "$target_dir" || true

echo "Launcher installed at $target"
