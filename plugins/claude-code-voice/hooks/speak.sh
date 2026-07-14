#!/usr/bin/env bash
# claude-code-voice -- Stop hook.
#
# Claude Code has voice INPUT (/voice) but no voice output. This hook is the
# missing half: when Claude finishes a turn, it reads the reply aloud.
#
# Pipeline:
#   Stop payload -> last_assistant_message -> strip markdown -> engine -> audio
#
# The message comes from the payload's `last_assistant_message` field, NOT from
# parsing transcript.jsonl -- that format is internal to Claude Code and is
# documented to change between releases.
#
# Engines:
#   kokoro  local neural TTS via a resident daemon (default; best quality)
#   say     the OS built-in (macOS `say`, Linux `espeak-ng`) -- zero deps
#
# Control: `claude-voice on|off|voice|engine|...`  (see bin/claude-voice)

set -u

VOICE_HOME="${CLAUDE_VOICE_HOME:-$HOME/.claude/voice}"
STATE_FILE="$VOICE_HOME/state"
ENGINE_FILE="$VOICE_HOME/engine"
VOICE_FILE="$VOICE_HOME/voice"
RATE_FILE="$VOICE_HOME/rate"
MAXLEN_FILE="$VOICE_HOME/maxlen"
PID_FILE="$VOICE_HOME/say.pid"
VENV_PY="$VOICE_HOME/venv/bin/python"

# AF_UNIX paths are capped near 104 bytes on macOS. A socket inside VOICE_HOME
# overflows that for deep home dirs, and bind() dies with "path too long" --
# so the daemon never starts and we silently degrade to the OS voice. Keep the
# socket short: a 0700 dir under /tmp, keyed by a hash of VOICE_HOME.
# voice-daemon.py computes the identical path; keep the two in sync.
_ccv_sock() {
  local h
  if command -v md5 >/dev/null 2>&1; then
    h=$(printf '%s' "$VOICE_HOME" | md5 -q 2>/dev/null | cut -c1-8)
  else
    h=$(printf '%s' "$VOICE_HOME" | md5sum 2>/dev/null | cut -c1-8)
  fi
  local d="/tmp/ccvoice-$(id -u)"
  mkdir -p "$d" 2>/dev/null && chmod 700 "$d" 2>/dev/null
  printf '%s/%s.sock' "$d" "$h"
}
SOCK="$(_ccv_sock)"

HOOK_DIR="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
[ -d "$HOOK_DIR/hooks" ] && HOOK_DIR="$HOOK_DIR/hooks"
DAEMON="$HOOK_DIR/voice-daemon.py"

# --- Off switch: cheapest possible early exit -------------------------------
[ "$(cat "$STATE_FILE" 2>/dev/null || echo off)" = "on" ] || exit 0

command -v jq >/dev/null 2>&1 || exit 0

# Settings live in files, not env vars: Claude Code spawns hooks without the
# interactive shell's environment, so an `export` in ~/.zshrc never reaches us.
ENGINE="${CLAUDE_VOICE_ENGINE:-$(cat "$ENGINE_FILE" 2>/dev/null || echo kokoro)}"
RATE="${CLAUDE_VOICE_RATE:-$(cat "$RATE_FILE" 2>/dev/null || echo 190)}"
# 0 = speak the whole reply. Streaming starts playback on sentence one, so a
# long answer costs no extra wait; a cap would only lose you the ending.
MAXLEN="${CLAUDE_VOICE_MAXLEN:-$(cat "$MAXLEN_FILE" 2>/dev/null || echo 0)}"

if [ "$ENGINE" = "kokoro" ]; then
  VOICE="${CLAUDE_VOICE_NAME:-$(cat "$VOICE_FILE" 2>/dev/null || echo af_heart)}"
else
  VOICE="${CLAUDE_VOICE_NAME:-$(cat "$VOICE_FILE" 2>/dev/null || echo Samantha)}"
fi

payload=$(cat)

# Stop hooks can re-fire after a hook-driven continuation. Don't re-speak.
[ "$(jq -r '.stop_hook_active // false' <<<"$payload")" = "true" ] && exit 0

message=$(jq -r '.last_assistant_message // empty' <<<"$payload")
[ -n "$message" ] || exit 0

# Identify the session so the daemon can tell parallel chats apart: barge in on
# ourselves, but queue (never clobber) another session's speech.
session=$(jq -r '.session_id // "unknown"' <<<"$payload")
cwd=$(jq -r '.cwd // empty' <<<"$payload")
label=""
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
  label=$(basename "$cwd")
  top=$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null || true)
  [ -n "$top" ] && label=$(basename "$top")
  br=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
  if [ -n "${br:-}" ] && [ "$br" != "HEAD" ] && [ "$br" != "main" ] && [ "$br" != "master" ]; then
    label="$label, $br"
  fi
fi

# --- Markdown -> speakable prose --------------------------------------------
# -CSD: decode I/O as UTF-8. Without it perl sees raw bytes and every \x{...}
# range below silently fails to match -- emoji get read out loud.
spoken=$(MAXLEN="$MAXLEN" perl -0777 -CSD -e '
  my $t = do { local $/; <STDIN> };

  # Fenced code blocks: announce, do not recite.
  $t =~ s/^[ \t]*```[^\n]*\n.*?^[ \t]*```[ \t]*$/\n(code block)\n/gms;
  $t =~ s/^[ \t]*```[^\n]*\n.*\z/\n(code block)\n/ms;   # unterminated fence

  # Harness-injected tags; never speak these.
  $t =~ s{<system-reminder>.*?</system-reminder>}{}gs;
  $t =~ s/<[^>\n]{1,80}>//g;

  # Links + images: keep the label, drop the URL.
  $t =~ s/!\[([^\]]*)\]\([^)]*\)/$1/g;
  $t =~ s/\[([^\]]*)\]\([^)]*\)/$1/g;

  # Inline code: keep the contents (file names carry meaning), drop the ticks.
  $t =~ s/`([^`\n]*)`/$1/g;

  # Emphasis, headers, quotes, rules, bullets, table pipes.
  $t =~ s/\*\*\*([^*]+)\*\*\*/$1/g;
  $t =~ s/\*\*([^*]+)\*\*/$1/g;
  $t =~ s/(?<!\w)[*_]([^*_\n]+)[*_](?!\w)/$1/g;
  $t =~ s/~~([^~]+)~~/$1/g;
  $t =~ s/^[ \t]*#{1,6}[ \t]*//gm;
  $t =~ s/^[ \t]*>[ \t]?//gm;
  $t =~ s/^[ \t]*([-*_])[ \t]*\1[ \t]*\1[-*_ \t]*$/ /gm;
  $t =~ s/^[ \t]*[-*+][ \t]+//gm;
  $t =~ s/^[ \t]*\d+[.)][ \t]+//gm;
  $t =~ s/\|/ /g;

  # Arrows carry meaning ("800ms -> 120ms"), so speak them, do not drop them.
  $t =~ s/\s*(?:\x{2192}|\x{27A1}|->)\s*/ to /g;

  # Emoji / box-drawing: read literally or mangled by every engine.
  $t =~ s/[\x{1F000}-\x{1FAFF}\x{2600}-\x{27BF}\x{2190}-\x{21FF}\x{2500}-\x{257F}]//g;
  $t =~ s/[\x{2018}\x{2019}]/'"'"'/g;
  $t =~ s/[\x{201C}\x{201D}]/"/g;
  $t =~ s/\x{2026}/. /g;
  $t =~ s/\x{2014}/, /g;

  # Collapse the whitespace the stripping left behind.
  $t =~ s/[ \t]+/ /g;
  $t =~ s/\n{2,}/. /g;
  $t =~ s/\n/. /g;
  $t =~ s/(?:\s*[.,]\s*){2,}/. /g;
  $t =~ s/:\s*\.\s*/: /g;
  $t =~ s/^\s+|\s+$//g;

  # Optional cap. 0 (the default) speaks the whole reply.
  my $max = $ENV{MAXLEN} // 0;
  if ($max > 0 && length($t) > $max) {
    $t = substr($t, 0, $max);
    if ($t =~ /^(.*[.!?])\s/s) { $t = $1 } else { $t =~ s/\s+\S*$// }
    $t .= " That is as far as I will read.";
  }
  print $t;
' <<<"$message")

[ -n "${spoken//[[:space:]]/}" ] || exit 0

# --- Engines ----------------------------------------------------------------

speak_builtin() {
  if command -v say >/dev/null 2>&1; then           # macOS
    say -v "$VOICE" -r "$RATE" -- "$1" >/dev/null 2>&1 &
  elif command -v espeak-ng >/dev/null 2>&1; then   # Linux
    espeak-ng -s "$RATE" -- "$1" >/dev/null 2>&1 &
  elif command -v espeak >/dev/null 2>&1; then
    espeak -s "$RATE" -- "$1" >/dev/null 2>&1 &
  else
    return 1
  fi
  echo $! > "$PID_FILE"
  disown 2>/dev/null || true
}

daemon_up() {
  [ -S "$SOCK" ] && [ "$(printf '__PING__\n' | nc -U "$SOCK" 2>/dev/null)" = "pong" ]
}

daemon_start() {
  [ -x "$VENV_PY" ] && [ -f "$DAEMON" ] || return 1
  nohup "$VENV_PY" "$DAEMON" >/dev/null 2>&1 &
  disown 2>/dev/null || true
  # Cold start = model load + first-synth warmup (~5s). Wait it out once.
  for _ in $(seq 1 60); do
    daemon_up && return 0
    sleep 0.25
  done
  return 1
}

speak_kokoro() {
  command -v nc >/dev/null 2>&1 || return 1
  daemon_up || daemon_start || return 1
  # One JSON line: session identity travels with the text.
  jq -cn --arg s "$session" --arg l "$label" --arg t "$1" \
    '{session:$s, label:$l, text:$t}' | nc -U "$SOCK" >/dev/null 2>&1
}

case "$ENGINE" in
  kokoro)
    # Fall back rather than leave the user in silence.
    speak_kokoro "$spoken" || speak_builtin "$spoken" || true
    ;;
  *)
    speak_builtin "$spoken" || true
    ;;
esac

exit 0
