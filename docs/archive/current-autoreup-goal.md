# Current AutoReup Goal

This file is the compact/handoff source of truth. Re-read it after context compaction before continuing implementation.

## Product goal

Turn a Chinese video into a Vietnamese-edited video with:

- one-line Vietnamese subtitles on video;
- Vietnamese voice-over/dubbing using a fixed CapCut voice;
- matching content between subtitles and voice-over;
- deterministic timing/QA gates before packaging.

This is not raw translation. It is a Vietnamese editorial/localization pipeline: understand Chinese source, preserve meaning and terms, write a concise Vietnamese master script, then derive subtitle display and TTS from that same script.

## Core data rule

Use one Vietnamese content source per semantic span:

```text
SOURCE_ZH + context/glossary + timing -> master_vi
master_vi -> display_1line cues
master_vi -> TTS audio
```

Do not maintain separate subtitle and narration translations. Subtitles and voice-over may have different segmentation, but should use the same wording/meaning.

## TTS voice and constraints

- Fixed voice: `BV421_vivn_streaming`.
- Verified catalog source: CapCut `/v2/speakers` returns real catalog, not fallback.
- Measured voice behavior on 30s sample:
  - median raw CPS: about `14.16` chars/sec;
  - safe budget: about `12-13` chars/sec.
- TTS raw synthesis should be neutral: speed/pitch/volume = `10` in CapCut wrapper.
- Local FFmpeg alignment only lightly adjusts tempo.
- Accept if `0.92 <= tempo_needed <= 1.18` and no semantic/glossary violation.
- If overflow, repair `master_vi` for failed IDs and resynthesize only those IDs when possible.

## Agent/LLM role

Agent writes or repairs Vietnamese `master_vi` only. Deterministic code owns timing, splitting, merging, QA, TTS duration checks, acceptance/rejection, muxing.

Agent input row should include:

```text
ID, duration, LIMIT_CHARS, GENRE, SOURCE_ZH, DRAFT_VI/CURRENT_VI, GLOSSARY, TTS failure reason when repairing
```

Agent output must be TSV only:

```text
ID<TAB>TEXT
```

## Display rule

Video subtitle display target is one line only. `display_resegment_subtitles.py` remains useful: feed it `master_vi`, generate 1-line display cues, never feed display-sliced cues into TTS.

## Current pilot artifacts

- Semantic sample input: `data/tts/bilibili-BV1kAHPzpERj.sample30s.semantic.json`
- Context/glossary: `data/subtitles/bilibili-BV1kAHPzpERj.sample5m.context.json`
- Real voice preview: `data/tts/capcut-bv421-vivn-preview.mp3`
- Real voice 30s baseline manifest: `data/tts/bilibili-BV1kAHPzpERj.sample30s.bv421-real.tts.json`
- Round 1 adapted script: `data/tts/bilibili-BV1kAHPzpERj.sample30s.bv421-adapted-r1.json`

## Production integration decisions

- Production now treats timed Vietnamese output as `master_vi` first, then derives display subtitles after timing cleanup.
- Final display subtitles are generated with `display_max_chars=32`, `display_max_lines=1`.
- TTS uses `master_json` when available, not display-sliced subtitles.
- TTS defaults are production CapCut defaults:
  - voice: `BV421_vivn_streaming`;
  - text mode: `continuous` to avoid punctuation pauses in voice-over;
  - target budget: `12 chars/sec`;
  - grouped synthesis enabled to avoid mid-sentence cuts.
- `dubbed_mp4` packaging now means final video with Vietnamese voice + Vietnamese soft subtitles, without original audio as an extra voice track.
- Prompt/agent work must not be blackbox: `create_master_vi_script.py` writes an audit JSON containing source units, genre, glossary/style notes, measured TTS budget, full system/user prompt, raw LLM output, parsed output, and final master rows.
- TTS budget is measured from the real CapCut voice with `scripts/calibrate_tts_voice.py`; prompt budgets use measured CPS/WPS, not blind fixed rules.
- Voice-over packaging keeps original audio as a low bed and ducks it under Vietnamese narration via FFmpeg sidechain compression; do not mute original audio by default.

## Immediate implementation target

Run a closed pilot loop on the 30s sample:

1. create/repair `master_vi` with source/context/TTS budget;
2. synthesize with `BV421_vivn_streaming`;
3. measure raw durations/overflow;
4. repair failed IDs only until overflows are acceptable or a deterministic limit is reached;
5. produce final artifacts and metrics.
