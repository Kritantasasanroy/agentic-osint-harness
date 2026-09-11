# Architecture

## The state machine

Six phases of the intelligence cycle, plus two terminal states. One `Step` is recorded per
transition, and that record is the sole source of truth for every metric.

```mermaid
stateDiagram-v2
    [*] --> DIRECTION

    DIRECTION --> COLLECTION: leads and competing hypotheses stated
    COLLECTION --> APPRAISAL: documents retrieved
    APPRAISAL --> RECONCILIATION: assertions extracted and graded
    RECONCILIATION --> REFLECTION: ACH scored, assessment appended
    REFLECTION --> COLLECTION: gaps remain or a high-priority lead is still open
    REFLECTION --> DISSEMINATION: evidence judged sufficient, or budget nearly spent
    DISSEMINATION --> COMPLETE: every citation traced to a retrieval

    DIRECTION --> HALTED: budget exhausted
    COLLECTION --> HALTED: budget exhausted
    APPRAISAL --> HALTED: budget exhausted
    RECONCILIATION --> HALTED: budget exhausted
    REFLECTION --> HALTED: budget exhausted
    DISSEMINATION --> HALTED: budget exhausted

    COMPLETE --> [*]
    HALTED --> [*]
```

The loop back from **Reflection** to **Collection** is what makes the investigation adaptive: new
leads discovered while reading send it out again, and it cannot converge while a high-priority
question is still open.

`HALTED` exists so a non-converging investigation ends visibly rather than silently. The halt is
itself recorded as a step, and a halted episode stays in the benchmark denominator with a failure
tag.

## What flows where

```mermaid
flowchart TB
    subgraph agent["The agent — may never see ground truth or its own reward"]
        direction TB
        MACHINE["InvestigationGraph<br/>routes, records one Step per transition, enforces the budget"]
        PHASES["Direction · Collection · Appraisal<br/>Reconciliation · Reflection · Dissemination"]
        BRIEF["Briefing<br/>filters state by memory mode"]
        MODEL["ModelClient<br/>LiveModel or RehearsedModel<br/>meters its own token spend"]
        MACHINE --> PHASES
        PHASES --> BRIEF
        PHASES --> MODEL
    end

    subgraph outside["External sources — every call recorded"]
        CASS["Cassette<br/>replay is the default; a miss is an error"]
        ENC["Encyclopedia"]
        PAGE["PageFetch"]
        CASS --- ENC
        CASS --- PAGE
    end

    subgraph mem["Long-term memory — conclusions only, never documents"]
        ARCH["InvestigationArchive<br/>lexical recall of past episodes"]
        REG["SourceRegister<br/>publisher grades carried forward"]
    end

    subgraph record["The investigation — append-only"]
        INV["Investigation<br/>documents · evidence · hypotheses<br/>assessments · steps"]
    end

    subgraph eval["Evaluation — runs only after the episode ends"]
        CASE["BenchmarkCase<br/>holds the expected answer"]
        SCORE["StepReward · EpisodeReward<br/>Diagnosis · BenchmarkRun"]
        REPORT["Dossier · Ablation"]
    end

    PHASES -->|"retrieve"| CASS
    ENC --> INV
    PAGE --> INV
    ARCH -->|"priors, labelled as recall"| BRIEF
    REG -->|"learned grades"| INV
    PHASES --> INV
    INV -->|"after COMPLETE or HALTED"| ARCH
    INV --> SCORE
    CASE -->|"subject only"| MACHINE
    CASE -->|"expected answer"| SCORE
    SCORE --> REPORT
    INV --> REPORT
```

Three boundaries in that picture are deliberate and enforced, not conventional:

**Ground truth stops at the evaluation box.** `BenchmarkCase` sends only its `subject` into the
agent. `Investigator.investigate()` has no overload that accepts a case, so the object holding the
expected answer cannot cross.

**Reward flows one way.** Nothing inside the agent imports anything from `bench`. Scoring reads the
finished investigation; the agent never reads the score.

**Memory carries conclusions, not documents.** The archive stores a digest with no URL in it, so
recalled material is structurally incapable of becoming a citation.

## Where each requirement lives

| Requirement from the brief | Where it is implemented |
| --- | --- |
| Investigate a company, person, or claim | `domain/subject.py` — one mechanism, polymorphic seeds |
| Gather from multiple external sources | `sources/tools.py`, plus server-side search in `model/live.py` |
| Plan and adapt as new information appears | `graph/phases.py` — `Reflection` reopens `Collection` |
| Evaluate evidence and source reliability | Admiralty grading in `domain/provenance.py` |
| Handle conflicting or insufficient information | ACH in `domain/analysis.py`; the sufficiency override in `Reconciliation` |
| Short-term and long-term memory | `graph/briefing.py` (in-episode), `memory/archive.py` (across episodes) |
| Reflect before concluding | `Reflection` phase |
| Structured report with citations and confidence | `report/dossier.py` |
| Maintain logs of the process | `Step`, appended once per transition, never mutated |
| Evaluate across multiple cases | `bench/`, `benchmark/cases.json`, `report/ablation.py` |

## Invariants the type system enforces

- Evidence cannot be recorded against a document the episode never retrieved.
- Assessments and steps are append-only, so confidence progression and convergence survive.
- An investigation holds at least two competing hypotheses once Direction has run.
- A report is not written at all while any citation lacks a document behind it.
- A conclusive verdict is overridden to insufficient evidence when the evidence cannot carry it.
- Every run is reproducible from `(case id, memory mode, cassette)`.
