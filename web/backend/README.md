---
title: Agentic OSINT Harness API
emoji: 🔎
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Agentic OSINT Harness, API

Replay-only HTTP surface over an autonomous OSINT investigator and its benchmark. Pick a company, a
person, or a claim, and the agent runs six phases over it, grades what it finds, weighs competing
explanations, and returns a cited report with a calibrated confidence.

Everything here replays from a recorded cassette. No API key, no live model, no network calls out.
That is structural, not a setting: the service never constructs a live model, so it cannot spend
quota or leak a credential.

## Endpoints

`GET /api/health` reports liveness and how many cases loaded.

`GET /api/cases` lists all 14 benchmark subjects, with the trap each one is built to catch.

`POST /api/investigate` takes `{"case_id": "...", "memory": "none|short|long"}` and returns the
scored outcome, the full trajectory, and the rendered analyst report.

`GET /api/ablation` runs all 14 cases across all three memory modes and returns the comparison.

Interactive docs at `/docs`. Source at
https://github.com/Kritantasasanroy/agentic-osint-harness
