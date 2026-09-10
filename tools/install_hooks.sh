#!/usr/bin/env bash
# Install the privacy guard as git hooks.
#
# Hooks live in .git/hooks, which git does not track -- so this script
# is the tracked part, and anyone cloning the repo runs it once:
#
#     ./tools/install_hooks.sh
#
# After that, a commit that would publish the name of a street on your
# round is refused, whether the name is in the diff or in the message.
set -euo pipefail
cd "$(dirname "$0")/.."
hooks="$(git rev-parse --git-path hooks)"
mkdir -p "$hooks"

# whichever python has this project's dependencies
python="python3"
[ -x map/bin/python ] && python="$PWD/map/bin/python"
[ -x .venv/bin/python ] && python="$PWD/.venv/bin/python"

cat > "$hooks/pre-commit" <<HOOK
#!/usr/bin/env bash
exec "$python" "$PWD/tools/privacy_guard.py" --staged
HOOK

cat > "$hooks/commit-msg" <<HOOK
#!/usr/bin/env bash
exec "$python" "$PWD/tools/privacy_guard.py" --message "\$1"
HOOK

chmod +x "$hooks/pre-commit" "$hooks/commit-msg"
echo "installed:"
echo "  $hooks/pre-commit   checks what the commit would add"
echo "  $hooks/commit-msg   checks the commit message"
echo "using $python"
