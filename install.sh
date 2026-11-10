#!/bin/sh
# Link aitop into ~/.local/bin. Run it again after moving the clone.
set -eu
dir=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.local/bin"
ln -sfn "$dir/aitop" "$HOME/.local/bin/aitop"
echo "linked $HOME/.local/bin/aitop -> $dir/aitop"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: $HOME/.local/bin is not on PATH" ;;
esac
