# claude-code-voice

**Claude Code can hear you. This makes it talk back.**

Claude Code ships voice *input*: press a key, dictate your prompt. But there's no voice *output*: when Claude finishes working, the answer just lands silently in your terminal, and you have to go read it.

That breaks the hands-free loop. You can talk to Claude from across the room, but you can't hear what it said.

This plugin closes it. When Claude finishes a turn, its reply is read aloud, using [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), a neural TTS model that runs **entirely on your machine**. No extra API key, no audio leaving your laptop, no per-word billing for the privilege of being read to.

It works in **any terminal**: iTerm2, Terminal.app, Ghostty, Warp, VS Code, cmux, tmux, a plain SSH session. It hangs off Claude Code's `Stop` hook, which fires the same way everywhere, so nothing in it knows or cares where you're running.

```
you dictate  ──▶  Claude works  ──▶  Claude speaks
     ▲                                     │
     └─────────────────────────────────────┘
```

---

## Install

```bash
/plugin marketplace add syymza/claude-code-voice
/plugin install claude-code-voice
```

Then install the CLI and the local voice model:

```bash
git clone https://github.com/syymza/claude-code-voice
cd claude-code-voice && ./install.sh
```

And turn it on:

```bash
claude-voice on
```

That's it. The next thing Claude says, you'll hear.

> **Not sold on a 520MB download?** `./install.sh --no-kokoro` skips the model and uses your OS's built-in voice (macOS `say`, Linux `espeak-ng`). It sounds dated, but it's instant and weighs nothing. Upgrade any time with `claude-voice install-kokoro`.

---

## Usage

```bash
claude-voice on              # read replies aloud
claude-voice off             # stop
claude-voice toggle          # flip it
claude-voice stop            # shut up right now, stay enabled

claude-voice replay          # say that again
claude-voice voices          # list the 27 Kokoro voices
claude-voice voice af_bella  # switch voice
claude-voice speed 1.15      # talk faster
claude-voice test            # hear a sample
claude-voice status          # what's on, what's warm
```

Everything takes effect on the **next reply**. No restart, no config editing.

### Or just say it

The plugin ships a skill, so you don't have to remember any of the above. Talk to Claude the way you already do:

> *"read that again"* · *"stop talking"* · *"a bit slower"* · *"use a British voice"* · *"which voice is this project?"* · *"turn the voice off"*

Which matters more than it sounds: the entire premise is that you're hands-free and not looking at the terminal. Asking it to slow down by *saying so* is the natural gesture. Typing `claude-voice speed 0.9` rather defeats the point.

`read that again` is the one you'll reach for most. A plane goes over, someone talks to you mid-answer, and the reply is gone. The daemon keeps the last thing it said per session precisely so it can repeat it.

> The readback itself stays a hook, not a skill, and has to. Skills are invoked *by the model* mid-conversation; readback must fire when a turn **ends**, the exact moment the model has stopped deciding anything. Only the harness can do that. The skill is the steering wheel, not the engine.

`af_heart` is the default and Kokoro's best-graded voice. `af_bella` and `am_michael` are the next best; `bf_emma` and `bm_george` are British.

---

## What makes it usable

Three problems that aren't obvious until you hit them, and what this does about them.

### It starts talking in ~1 second, however long the reply

The naive approach (synthesize the whole reply, then play it) means **time-to-first-sound scales with how much Claude said**. A long answer left us waiting 4+ seconds in silence, and it got worse the more Claude wrote.

So the daemon splits the reply into sentences and starts playing the first one while the rest synthesize behind it. First sound lands in about a second and *stays* there, no matter how long the answer runs.

There's also a resident daemon rather than a fresh process per reply. Not for the model load (that's only ~0.4s), but because ONNX's **first** synthesis costs ~5s in graph warmup. A one-shot process pays that every single turn. The daemon pays it once, at startup, into silence.

### It handles parallel sessions without talking over itself

If you run several Claude Code sessions at once (cmux tabs, git worktrees, parallel agents), they all finish turns independently, and a naive "interrupt whatever's playing" rule means **whichever session finishes last cuts off whatever another was mid-sentence saying**, with no clue whose voice you were even hearing.

Instead:

| | |
|---|---|
| **Same session, new turn** | Barges in. You want its latest answer, not a stale backlog |
| **Different session** | Queues behind, and announces itself by project name |

So two chats finishing together get read one after the other, in full, and you know which is which.

Better still, **give each project its own voice**:

```bash
claude-voice voicemode project
```

Now `api-server` always speaks in one voice and `data-pipeline` in another, and you can follow both by ear without waiting to hear a name. Voices are keyed on the project + branch, not the session id (which is regenerated on every restart and would reshuffle everything daily), so a context sounds the same tomorrow as it does today, and two worktrees of the same repo get *different* voices, which is the case that's hardest to tell apart.

The pool is eight voices, deliberately alternating gender and accent so neighbours never sound alike. Assignment is least-used-first rather than hashed: hashing collides badly at small N (ten projects over eight voices put three of them on the same voice, defeating the point).

```bash
claude-voice whospeaks                      # who sounds like what
claude-voice voicefor api-server bm_george  # pin one
claude-voice announce always|smart|off      # spoken project names
```

Labels that are just opaque ids (UUIDs, hashes, bare digits) are never read aloud, because "seven b, three f, nine a" is worse than hearing nothing.

### It doesn't read markdown at you

Piping Claude's raw output into a TTS engine is unbearable. It recites backticks, asterisks, table pipes, and forty seconds of TypeScript. So the reply is stripped first: code fences become a brief *"code block"* rather than a recital, emphasis and headers and list bullets vanish, links keep their text and lose their URL, and emoji are dropped instead of being read out as *"party popper"*.

Arrows survive as words, though. `800ms -> 120ms` becomes *"800ms to 120ms"*, because deleting it would have turned a real claim into nonsense.

---

## How it works

Claude Code's `Stop` hook fires once per completed turn and hands you the reply in `last_assistant_message`:

```
Stop hook ──▶ speak.sh ──▶ strip markdown ──▶ unix socket ──▶ voice-daemon.py
                                                                    │
                                            chunk ─▶ synthesize ─▶ play
                                                    (Kokoro)     (afplay)
```

The hook returns in ~0.1s. It hands the text to the daemon and gets out of the way, so Claude is never blocked waiting on audio.

> **Note:** the reply text comes from the hook payload's `last_assistant_message` field, **not** from parsing `transcript.jsonl`. That transcript format is internal to Claude Code and documented to change between releases; parsing it is a latent break on any upgrade.

If Kokoro fails to load for any reason, the hook silently falls back to the OS voice rather than leaving you in silence.

---

## Requirements

- macOS or Linux
- `jq`, `perl`, `nc`, `curl`
- An audio player: `afplay` (macOS), `paplay`/`aplay` (Linux)
- For the neural engine: `uv` or `python3`, and ~520MB of disk

---

## Configuration

State lives in files under `~/.claude/voice/`, not environment variables. Claude Code spawns hooks **without your interactive shell's environment**, so an `export` in `~/.zshrc` would never reach the hook. (That one cost an afternoon.)

| Command | Default | |
|---|---|---|
| `claude-voice engine kokoro\|say` | `kokoro` | Neural, or the OS built-in |
| `claude-voice voice <name>` | `af_heart` | See `claude-voice voices` |
| `claude-voice speed <x>` | `1.0` | Kokoro rate multiplier |
| `claude-voice maxlen <n>` | `0` | Cap spoken characters; `0` = the whole reply |
| `claude-voice voicemode project\|fixed` | `fixed` | Give each project its own voice |
| `claude-voice announce always\|smart\|off` | `smart` | Speak the project name in parallel sessions |

---

## Troubleshooting

**Nothing is spoken.** Check `claude-voice status`. Readback may be `off`. If it's on, `claude-voice logs` shows what the daemon did.

**The first reply took ~5 seconds.** That's a cold daemon paying ONNX's one-time warmup. Every reply after is ~1s. `claude-voice restart` warms it deliberately.

**It talks over itself.** Shouldn't happen, so file an issue with `claude-voice logs`.

**I want it to shut up right now.** `claude-voice stop` silences the current reply without disabling readback.

---

## Credits

- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) by hexgrad, Apache 2.0
- [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) by thewh1teagle, MIT

Model weights are downloaded at install time, not vendored here.

## License

MIT
