#!/usr/bin/env bash
# claude-code-voice — installer.
#
# Puts the `claude-voice` CLI on your PATH and (by default) installs the local
# Kokoro neural TTS engine. The Claude Code hook itself is wired up by the
# plugin — see the README for `/plugin marketplace add`.
#
#   ./install.sh                 CLI + kokoro engine (~520MB)
#   ./install.sh --no-kokoro     CLI only; uses the OS built-in voice
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_SRC="$REPO_DIR/plugins/claude-code-voice/bin/claude-voice"
VOICE_HOME="${CLAUDE_VOICE_HOME:-$HOME/.claude/voice}"

WITH_KOKORO=1
[ "${1:-}" = "--no-kokoro" ] && WITH_KOKORO=0

say_step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
warn()     { printf '\033[33m! %s\033[0m\n' "$1"; }

say_step "Checking prerequisites"
missing=()
for cmd in jq perl nc curl; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if [ ${#missing[@]} -gt 0 ]; then
  warn "missing: ${missing[*]}"
  echo "  macOS:  brew install ${missing[*]}"
  echo "  Debian: sudo apt install ${missing[*]}"
  exit 1
fi
if ! command -v afplay >/dev/null 2>&1 \
  && ! command -v paplay >/dev/null 2>&1 \
  && ! command -v aplay  >/dev/null 2>&1; then
  warn "no audio player found (afplay / paplay / aplay). Kokoro needs one."
fi
echo "  ok"

say_step "Installing the claude-voice CLI"
BIN_DIR=""
for candidate in "$HOME/.local/bin" "/usr/local/bin"; do
  if [ -d "$candidate" ] && [ -w "$candidate" ]; then BIN_DIR="$candidate"; break; fi
done
if [ -z "$BIN_DIR" ]; then
  BIN_DIR="$HOME/.local/bin"
  mkdir -p "$BIN_DIR"
fi
ln -sf "$CLI_SRC" "$BIN_DIR/claude-voice"
echo "  linked $BIN_DIR/claude-voice -> $CLI_SRC"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH — add it to your shell profile:"
     echo "      export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac

mkdir -p "$VOICE_HOME"

if [ "$WITH_KOKORO" = 1 ]; then
  say_step "Installing the Kokoro neural engine (~520MB)"
  echo "  Skip this with: ./install.sh --no-kokoro"
  "$BIN_DIR/claude-voice" install-kokoro
else
  say_step "Skipping Kokoro — using the OS built-in voice"
  echo say > "$VOICE_HOME/engine"
  echo "  Install it later with: claude-voice install-kokoro"
fi

say_step "Done"
cat <<EOF

Wire up the Claude Code plugin (this is what fires the hook):

    /plugin marketplace add syymza/claude-code-voice
    /plugin install claude-code-voice

Then, in any Claude Code session:

    claude-voice on        # start reading replies aloud
    claude-voice test      # hear a sample
    claude-voice voices    # pick a different voice
    claude-voice off       # stop

EOF
