# Codex project guide

Read this file first. Open only the files relevant to the requested change; do not scan
`downloads/`, `models/`, `dist/`, virtual environments, logs, generated subtitles, or the
archived long guide unless the task explicitly needs them.

## Purpose and invariants

- Windows-local YouTube workflow: download → English source/Whisper → AI translation →
  bilingual render → optional Bilibili upload.
- Preserve source video, source audio, original subtitles, IDs, and timelines.
- Never print, return, commit, or write API keys into task artifacts. Local secrets belong
  only in root `.env`; the panel returns configured booleans only.
- Download requires rights confirmation. Do not weaken `selected` / `rights_status` gates.
- Preserve checkpoints and backward-compatible artifact names unless a migration is part of
  the task.
- Translation quality is owned by the selected API model. Do not add heuristic semantic QC,
  local model QC, or automatic quality-based second-pass translation. Structural validation
  (JSON, IDs, non-empty translations, unchanged timeline, legal control characters) remains.
- Explicit `--polish-all` is the only normal second translation pass.
- API overload (`429`, including Zhipu `1305`) uses long explicit backoff and writes the
  current batch checkpoint before sleeping. Do not restore hidden SDK retries or automatic
  paid-provider fallback.
- DeepSeek translation defaults to thinking disabled. Empty content, truncated JSON, and
  missing/structurally contaminated IDs use response-level fallback: checkpoint usage/finish
  reason, retry only pending IDs, isolate contaminated IDs to one-item requests, disable
  thinking/native JSON mode, and reduce request size. Preserve this degradation.
  Incomplete enabled-thinking checkpoints may preserve valid rows when switching to disabled;
  completed checkpoints must still invalidate normally.
- The working tree may contain user changes. Do not discard or rewrite unrelated changes.

## Canonical entry points

| Area | Start here |
|---|---|
| Control panel backend | `src/control_panel/app.py`, `server.py`, `jobs.py` |
| Control panel UI | `src/control_panel/static/index.html`, `app.js`, `styles.css` |
| Provider/model registry | `src/stage3/llm_providers.py` |
| LLM adapter, prompt, usage, batch checkpoints | `src/stage3/translator_deepseek.py` |
| Stage 3 orchestration | `src/stage3/pipeline.py`, `config_adapter.py` |
| Stage 4 rendering | `src/stage4/`, `src/run_stage4.py` |
| Download | `src/download_core.py`, `src/download_video.py` |
| Tests | `tests/test_control_panel.py`, `tests/stage3/`, `tests/stage4/` |
| User guides | `README.md`, `PORTABLE_README.md` |

`translator_deepseek.py` and internal source value `deepseek` retain legacy names, but now
mean the provider-neutral AI translation path. Do not rename them casually; old checkpoints,
tests, BAT files, and task scanners rely on them.

## LLM configuration

The active provider is selected by `TRANSLATION_PROVIDER`; provider keys and model choices
are declared only in `src/stage3/llm_providers.py`. The web panel saves:

```text
TRANSLATION_PROVIDER / MODEL / BASE_URL / THINKING
TRANSLATION_BATCH_SIZE / CONTEXT_BEFORE / CONTEXT_AFTER / MAX_OUTPUT_TOKENS
```

OpenAI-compatible providers use the OpenAI client. Anthropic uses native `/v1/messages`.
Default token policy is batch 32, context 2/2, max output 4096, thinking disabled, compact
JSON prompt, dynamic output limit, pending-ID-only retry, and no automatic polish.

Stage 4 defaults to one visual line per language. It collapses source SRT wrapping at render
time, keeps one explicit language separator, disables ASS auto-wrap, and fits long lines by
font size then mild horizontal scaling. The `*_1080p` font sizes scale by frame height only;
do not restore aspect-ratio enlargement. At 1080p the configured display target is Chinese
60px / English 44px and the publishable floor is Chinese 54px / English 40px; do not restore
the rejected 24/20px tier. Prefer large-font pagination, then the publishable floor only when
the cue is too short. Keep exactly one line per language at every instant. If pages would be
too brief or still cannot fit, write preview/QC, return `REVIEW_REQUIRED`, and stop before
FFmpeg; never render a knowingly cropped, tiny, or unreadably flashing output.
Stage 3 structural QC also rejects per-ID payload overflow (several captions concatenated into
one ID) without judging wording or automatically buying a semantic second pass. Do not change
Stage 3 subtitle text merely to solve visual layout.

When adding a provider, update the registry, panel catalog behavior, `.env.example`, README
table, and offline adapter/settings tests. Never hardcode a real key.

## Verification

Use the project venv; the system Python may lack dependencies:

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.venv\Scripts\python.exe -m compileall -q src tests
node --check src\control_panel\static\app.js
git diff --check
```

Focused translation/panel tests:

```bat
.venv\Scripts\python.exe -m unittest tests.stage3.test_translator tests.stage3.test_stage3_pipeline tests.test_control_panel
```

Tests must be offline. Mock LLM, YouTube, FFmpeg, and publishing calls. For changes to deletion
or file movement, resolve targets under the intended project/task directory before mutation.

## Documentation routing

- `README.md`: current setup, panel workflow, providers, output, troubleshooting, security.
- `PORTABLE_README.md`: packaged CPU/GPU distribution.

Keep this file small and operational so Codex can orient without loading the full repository.
