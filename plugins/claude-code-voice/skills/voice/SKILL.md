---
name: voice
description: >-
  Control spoken readback of Claude's replies. Turn it on or off, silence it,
  replay the last answer, change the voice or the speed, or give each project
  its own voice. Use whenever the user refers to hearing, speaking, reading
  aloud, the voice, or being read to. Triggers include stop talking, read that
  again, say that slower, use a British voice, turn the voice off, why are you
  speaking, and which voice is this project.
---

# Voice readback control

The user has `claude-code-voice` installed: a `Stop` hook that reads your replies
aloud through a local neural TTS engine. This skill is how they steer it by
speaking instead of typing flags, which matters because the whole point of the
tool is that they are hands-free and probably not looking at the terminal.

Every action is a `claude-voice` shell command. Run it, then confirm in **one
short sentence**. The confirmation is about to be read aloud, so do not
narrate, do not list options, and do not restate what they asked.

## Mapping intent to commands

| The user says | Run |
|---|---|
| "read that again", "say that again", "I missed that" | `claude-voice replay` |
| "stop", "be quiet", "shut up", "stop talking" | `claude-voice stop` |
| "turn the voice off", "no more reading" | `claude-voice off` |
| "read your replies to me", "turn the voice on" | `claude-voice on` |
| "slower" / "faster" | `claude-voice speed <x>` (see below) |
| "use a British voice", "use a male voice", "different voice" | `claude-voice voice <name>` |
| "what voices are there" | `claude-voice voices` |
| "give each project its own voice" | `claude-voice voicemode project` |
| "which voice is this project", "who is speaking" | `claude-voice whospeaks` |
| "always tell me which project" | `claude-voice announce always` |
| "stop saying the project name" | `claude-voice announce off` |
| "is the voice on?", "why aren't you speaking" | `claude-voice status` |

## Speed

`speed` is a multiplier, default `1.0`. Read the current value from
`claude-voice status`, then step by ~0.15 in the direction asked, clamped to the
range 0.5 to 2.0. "Much faster" is a bigger jump. Do not ask them for a number.

## Voices

Kokoro voice names encode accent and gender: `af_*` US female, `am_*` US male,
`bf_*` UK female, `bm_*` UK male. So "a British man" is `bm_george` or
`bm_lewis`; "an American woman" is `af_heart` or `af_bella`. Pick a sensible one
that matches the request and set it. Only run `claude-voice voices` if they
explicitly want the list.

Good defaults: `af_heart` (best overall), `af_bella`, `am_michael`, `bf_emma`,
`bm_george`.

## Replay

`claude-voice replay` re-speaks the last reply. Reach for it on any "I missed
that" phrasing. It is *not* a summary and not a rephrase. Do not re-answer the
question, just replay it.

## Notes

- If a command reports that the daemon is not running, run `claude-voice restart`
  once and retry.
- If the user is clearly *not* talking about speech (e.g. "voice of the
  customer", a `voice` variable in their code), this skill does not apply.
- Never enable readback unprompted. If it is off, it is off on purpose.
