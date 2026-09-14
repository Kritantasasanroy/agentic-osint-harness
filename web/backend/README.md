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

The HTTP service behind the hosted demo. Pick a benchmark company, person or claim, or upload a
document, and the agent investigates it live: a real model, real web search, real page retrieval,
six phases, and a cited report with a calibrated confidence.

Nothing on the investigation path is replayed or rehearsed. With no API key configured, or with the
day's shared allowance of live calls spent, a request is refused with the reason stated, never
quietly answered some other way. The one deliberate exception is the memory ablation, which always
replays the recorded cassette, because comparing memory modes is only valid when every arm sees
identical retrieval.

## Endpoints

`GET /api/health` reports liveness, how many cases loaded, and what is left of today's live allowance.

`GET /api/cases` lists all 14 benchmark subjects, with the trap each one is built to catch.

`POST /api/investigate` takes `{"case_id": "...", "memory": "none|short|long"}` and returns a job id
to poll.

`POST /api/documents?filename=report.pdf` takes the raw bytes of one PDF, Word (.docx), HTML,
Markdown or plain text file, up to 5 MB, and returns a job id. The job reads the one checkable claim
out of the document, then investigates that claim against independent sources, never against the
document itself. A file that cannot be read is refused straight away with the reason.

`GET /api/jobs/{job_id}` reports the running phase while a job works, and once it finishes, the full
investigation record and report, plus the scored outcome for a benchmark case or the verdict for a
document.

`GET /api/ablation` runs all 14 cases across all three memory modes, replayed, and returns the
comparison.

## Configuration

`NVIDIA_API_KEY` (preferred) or `OPENROUTER_API_KEY` for the model, and `TAVILY_API_KEY` for search.
`LIVE_CALLS_PER_DAY` and `LIVE_CALLS_PER_VISITOR` cap the shared allowance, and `LIVE_MODEL` overrides
the model id.

Interactive docs at `/docs`. Source at
https://github.com/Kritantasasanroy/agentic-osint-harness
