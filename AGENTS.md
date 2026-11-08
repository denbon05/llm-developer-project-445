# AGENTS.md

- Course steps are in `docs/steps/` (git-ignored, start with its README). Never copy course text into committed files.
- One course step means one spec `docs/specs/NN-<topic>.md` and one commit `feat(step-N): ...`. The spec is the source of truth: update it when the code deviates.
- Don't stage or commit. The developer reviews and stages every file.
- Keep the `Makefile` target names and `.github/workflows/hexlet-check.yml`. The Hexlet autotests depend on them.
- Text the subscriber sees (replies, cards, refusals) is in Russian, because the tests match it. Mark every other Russian-dependent spot with `# I18N: <what changes>` for the later move to English.
- After changing dependencies run `make install`: it regenerates `requirements.txt`. Hexlet's checker has no uv: it installs that file with pip and runs the Makefile targets with plain `python`.
