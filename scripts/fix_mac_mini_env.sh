#!/usr/bin/env bash
# Discover Node/npm on Mac Mini (launchd/non-interactive shells often have a bare PATH).
# Usage:
#   source scripts/fix_mac_mini_env.sh   # current shell
#   bash scripts/fix_mac_mini_env.sh     # print findings + suggested ~/.zshrc lines

set -euo pipefail

echo ""
echo "IG Agent — Mac Mini PATH fix"
echo "============================"
echo ""

FOUND=()
add_path() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  case " ${FOUND[*]:-} " in
    *" $dir "*) return 0 ;;
  esac
  FOUND+=("$dir")
}

# Common Homebrew / system locations
for dir in \
  /opt/homebrew/bin \
  /opt/homebrew/sbin \
  /usr/local/bin \
  /usr/local/sbin \
  /usr/bin \
  /bin
do
  add_path "$dir"
done

# nvm
if [[ -d "${NVM_DIR:-$HOME/.nvm}/versions/node" ]]; then
  while IFS= read -r -d '' node_bin; do
    add_path "$(dirname "$node_bin")"
  done < <(find "${NVM_DIR:-$HOME/.nvm}/versions/node" -maxdepth 2 -name node -type f -print0 2>/dev/null || true)
fi

# fnm
if [[ -d "${FNM_DIR:-$HOME/.local/share/fnm}" ]]; then
  while IFS= read -r -d '' node_bin; do
    add_path "$(dirname "$node_bin")"
  done < <(find "${FNM_DIR:-$HOME/.local/share/fnm}" -maxdepth 4 -name node -type f -print0 2>/dev/null || true)
fi
if [[ -d "$HOME/.fnm" ]]; then
  while IFS= read -r -d '' node_bin; do
    add_path "$(dirname "$node_bin")"
  done < <(find "$HOME/.fnm" -maxdepth 4 -name node -type f -print0 2>/dev/null || true)
fi

# asdf / volta
for dir in \
  "$HOME/.asdf/shims" \
  "$HOME/.volta/bin"
do
  add_path "$dir"
done

# Sort newest nvm/fnm node first (version dirs often sort lexically)
if ((${#FOUND[@]} == 0)); then
  echo "No candidate bin directories found."
  echo "Install Node via Homebrew: brew install node"
  echo "Or nvm: https://github.com/nvm-sh/nvm"
  exit 1
fi

echo "Discovered directories:"
for dir in "${FOUND[@]}"; do
  node="$dir/node"
  npm="$dir/npm"
  if [[ -x "$node" ]]; then
    ver="$("$node" --version 2>/dev/null || echo "?")"
    echo "  ✓ $dir  (node $ver)"
  elif [[ -x "$npm" ]]; then
    echo "  ✓ $dir  (npm present, node missing)"
  else
    echo "  · $dir"
  fi
done
echo ""

# Prepend discovered dirs (preserve order, dedupe)
NEW_PATH=""
for dir in "${FOUND[@]}"; do
  NEW_PATH="${dir}${NEW_PATH:+:}${NEW_PATH}"
done
NEW_PATH="${NEW_PATH}${PATH:+:${PATH}}"

export PATH="$NEW_PATH"

if command -v node >/dev/null 2>&1; then
  echo "Active node: $(command -v node) ($(node --version))"
else
  echo "WARN: node still not on PATH after export."
fi
if command -v npm >/dev/null 2>&1; then
  echo "Active npm:  $(command -v npm) ($(npm --version))"
else
  echo "WARN: npm still not on PATH after export."
fi
echo ""

echo "Add to ~/.zshrc (pick the lines that match your install):"
echo "---"
for dir in "${FOUND[@]}"; do
  [[ -x "$dir/node" || -x "$dir/npm" ]] || continue
  echo "export PATH=\"$dir:\$PATH\""
done
echo "# Or for nvm (interactive shells):"
echo 'export NVM_DIR="$HOME/.nvm"'
echo '[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"'
echo "---"
echo ""
echo "Dashboard build:"
echo "  cd \"$(cd "$(dirname "$0")/.." && pwd)/dashboard\" && npm run build"
echo ""
