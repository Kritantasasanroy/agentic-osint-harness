# Agentic Harness for OSINT

An autonomous investigator that takes a company, a person, or a factual claim, searches and reads
across multiple external sources, weighs what it finds against competing explanations, and returns a
findings report with citations and a calibrated confidence — together with a benchmark and an
instrumented evaluation harness that measures whether any of that is actually working.

The agent is an explicit state machine. Every transition is recorded, and every number in the
evaluation is recomputed from those records rather than reported from memory.

---

## Run it in two minutes, with no API key

The whole benchmark replays from a recorded cassette, so a reviewer can run everything offline.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"

osint-harness cases                                  # the 14 benchmark subjects and claims
osint-harness investigate ada-lovelace-person        # one investigation, replayed offline
osint-harness ablate                                 # all 14 cases x 3 memory modes, with a report
pytest && mypy && ruff check .                       # 216 tests, strict types, clean lint
```

Reports land in `runs/<case>-<memory>/findings.md` and the ablation in `runs/results/ablation.md`.

**What you are running offline is the machinery, not the analysis.** Without an API key the harness
uses a *rehearsed analyst*: it performs real retrieval and produces real citations, but does no
reasoning, so it abstains on every case. That is deliberate — see
[Honest status](#honest-status-what-is-and-is-not-verified). To run the real thing:

```bash
export ANTHROPIC_API_KEY=...
osint-harness --live --record ablate
```

---

## How it works

Six phases, named for the stages of the intelligence cycle, wired as a state machine:

```
DIRECTION -> COLLECTION -> APPRAISAL -> RECONCILIATION -> REFLECTION -> DISSEMINATION -> COMPLETE
                  ^                                            |
                  +--------- more evidence needed -------------+
```

| Phase | What it does |
| --- | --- |
| **Direction** | States the lines of enquiry and at least two competing hypotheses. |
| **Collection** | Searches, picks which results are worth opening, and retrieves documents. |
| **Appraisal** | Extracts assertions and grades both the publisher and each claim. |
| **Reconciliation** | Scores every piece of evidence against every hypothesis, and concludes. |
| **Reflection** | Criticises the investigation and decides whether concluding would be honest. |
| **Dissemination** | Writes the report, after proving every citation traces to a real retrieval. |

Full diagram and data flow: [`docs/architecture.md`](docs/architecture.md).
Design rationale, findings, and limitations: [`docs/report.md`](docs/report.md).
The domain model the code is audited against: [`docs/design/domain-model.md`](docs/design/domain-model.md).

---

## Decisions the brief left open

The brief said it cares more about choices than spec-following. These are the ones that mattered.

### 1. Borrow the domain from intelligence analysis instead of inventing scoring

Rather than invent a confidence number, the harness uses three instruments that already exist and
that a reviewer can check against an external standard:

- **The Admiralty (NATO) system** grades evidence on two separate axes: source reliability `A-F` for
  the publisher, information credibility `1-6` for the individual claim. Conflating the two is the
  most common misuse of the scale, so they live on different objects and cannot be mixed up.
- **Analysis of Competing Hypotheses (ACH)** resolves contradictions by ranking hypotheses by *least
  disconfirmed* rather than most supported. Evidence consistent with five explanations is weak;
  evidence that rules one out is strong. This is what makes the agent structurally resistant to
  confirmation bias rather than merely instructed against it.
- **ICD 203 words of estimative probability** turn confidence into a named band with a numeric
  range, so it can be Brier-scored instead of admired.

This also means every investigation — company, person, or claim — runs through **one** mechanism.
The subject kind only chooses the opening questions; it never branches the logic.

### 2. No orchestration framework, and no vector database

The state machine is about a hundred lines. Every metric the brief asks for is a hook on its loop:
one `Step` recorded per transition, carrying the phase, the reason it moved, what it called, and
what it spent. Taking a framework dependency would have meant instrumenting somebody else's
abstraction to recover numbers the hand-written version exposes for free, and would have hidden the
part of the project a reviewer most wants to see.

### 3. Record/replay cassettes are load-bearing, not a convenience

The memory ablation compares `none`, `short` and `long`. That comparison is only valid if all three
see **identical** evidence. Every external lookup is keyed and recorded, and replay is the default —
a cassette miss is an error, not a quiet fall-through to the network, because a silent live call
would destroy the guarantee the cassette exists to provide. A delta measured against live search
would be measuring the weather.

It also makes the entire benchmark runnable with no API key, which is why you could run it above.

### 4. Memory recall is lexical on purpose — the weakness *is* the experiment

Long-term memory retrieves prior investigations by lexical similarity on the subject description,
not by embedding. An embedding retriever that never confuses two people sharing a name would leave
the harmful-retrieval metric with **nothing to detect**, and same-name confusion is the canonical
way memory fails in open-source intelligence work. A test pins the confusion in place, and the
measured ablation duly shows memory interference appearing only in `long` mode.

### 5. Memory can never become evidence, and that is structural

The archive stores what an episode *concluded* — never what it read — and recalled material enters a
briefing explicitly labelled as unverified recall.

The guarantee does not rest on that labelling, nor on the archive's shape alone. It rests on one
enforced rule: `Evidence` has a single construction site, and `record_evidence` refuses any item
whose URL is not already in the documents this episode actually retrieved. An `Investigation` also
refuses to be constructed or reloaded holding one. An independent auditor broke an earlier version
of this claim — the archive's shape alone was not enough, because a record could be assembled
outside the guarded path — and the validator exists because of that.

### 6. Ground truth cannot reach a prompt, and that is also structural

`Investigator.investigate()` accepts a `Subject`. There is no overload that accepts a
`BenchmarkCase`, so the object carrying the expected answer never crosses into the agent. A test
runs a real case end to end and asserts that none of its expected findings, notes, or trap labels
appear in any prompt the model saw.

### 7. Refusal fallbacks are deliberately disabled

Server-side refusal fallbacks silently re-run a declined request on a different model. They are not
configured here, deliberately: enabling them would put episodes produced by **two different models**
into the same benchmark — the same class of error as comparing memory modes against different
evidence. A refusal is raised, the episode is tagged, and it stays in the denominator. For a product
that trade is usually worth making; for a measurement it is not.

### 8. Reward is decomposed, version-stamped, and computed offline

A scalar reward cannot answer "where did this break down", which is what the brief asks. So:

| Component | Weight | Why |
| --- | --- | --- |
| Correctness | 0.40 | Being right matters most. |
| Calibration | 0.25 | Half Brier, half "was the confidence defensible at all". Brier alone is minimised by hedging everything to even odds. |
| Evidence quality | 0.20 | Mean Admiralty weight, discounted when everything came from one publisher. |
| Citation integrity | 0.15 | A **gate**, not a gradient. A fabricated citation zeroes it. |
| Efficiency | capped penalty | So a cheap wrong answer cannot win. |

Reward is computed **after** an episode ends, from the stored trajectory. Nothing the agent can
reach imports the scoring code, so it cannot chase its own score. The formula carries a version
stamp; changing it bumps the version and invalidates every number carrying the old one.

### 9. Abstention is scored at zero in both directions

Abstaining where a verdict was available, and committing where abstention was the only honest
answer, both score **zero on correctness** — no partial credit, where a supported/partially-supported
miss gets half. The episode can still earn something on the other components, which is intended: an
agent that reached the wrong verdict but cited real, well-graded sources did something better than
one that invented them. Correctness is the largest single weight, so the penalty dominates.

Related: `INSUFFICIENT_EVIDENCE` is a **judgment**, not a hypothesis. A refusal to commit makes no
claim and therefore cannot be disconfirmed, so putting it in the ACH matrix would let "we don't
know" accumulate consistency scores against evidence it says nothing about.

---

## The benchmark

14 cases — 5 companies, 4 people, 5 claims — chosen to be adversarial rather than merely varied.
All four judgments and all seven traps are exercised, and a test fails if that stops being true.

| Case | Trap | Why it is there |
| --- | --- | --- |
| Vantage Nebula Holdings | insufficient evidence | Invented company. Any confident verdict is fabrication. |
| John Smith (no qualifiers) | same name | No identifiable subject; abstention is the only honest answer. |
| Michael Jordan (ML researcher) | disambiguation | Resolvable identity under a famous namesake — here abstention would be *wrong*. |
| The current King of France is bald | false premise | Neither true nor false as stated. |
| Einstein's Nobel "for relativity" | popular myth | Half true and misleading — forces the partially-supported verdict to be reachable. |
| Great Wall visible from space | popular myth | A myth with more sources than its correction; tests reliability over volume. |
| Theranos, Wirecard | adverse media | Findings the subject actively denied. |
| Apollo 11 landed in 1969 | none | A deliberate **floor**: an investigator that will not commit here is miscalibrated, not careful. |

---

## Honest status: what is and is not verified

**Verified, by commands you can re-run:** 216 tests pass, `mypy --strict` is clean across 40 source
files, `ruff check` is clean, and a full 42-episode ablation (14 cases x 3 memory modes) runs end to
end offline and writes its report. Running `ablate` twice produces byte-identical output, which is
what makes the numbers below quotable at all.

**Not verified:** the live model path. No `ANTHROPIC_API_KEY` was available in the environment this
was built in, so `LiveModel.decide()` and `LiveModel.search()` have **never executed**. Their
response parsing is tested; the call-and-charge glue is not. Every shipped number below comes from
the rehearsed analyst.

**What the shipped numbers therefore mean.** The rehearsed analyst abstains on every case, so it is
correct only on the three where abstention is the right answer — 3/14 = 21%. This validates the
machinery, not the agent:

| Memory | Accuracy | Mean reward | Irrelevant retrieval | Harmful retrieval | Memory-interference failures |
| --- | --- | --- | --- | --- | --- |
| `none` | 21% | 0.332 | 0% | 0% | 0 |
| `short` | 21% | 0.332 | 0% | 0% | 0 |
| `long` | 21% | 0.332 | 14% | 14% | 2 |

The one genuinely informative row is the last column: **`long` mode shows memory interference that
`none` and `short` do not**, on similarly-named subjects, detected and attributed automatically.
The instrumentation works even when the analyst behind it does not.

Reproducing the real numbers is one command and an API key: `osint-harness --live --record ablate`.

---

## Repository layout

```
src/osint_harness/
  domain/        subjects, provenance and Admiralty grading, evidence and ACH, the investigation
  graph/         the state machine, the six phases, the memory-gated briefing, reply schemas
  sources/       external sources behind the record/replay cassette
  model/         the reasoning client, live and scripted
  memory/        the cross-episode archive and the publisher register
  bench/         benchmark cases, reward, failure taxonomy, run aggregation
  report/        the analyst-facing dossier and the ablation report
benchmark/       the 14 cases, and the recorded cassette they replay from
docs/            architecture, design rationale, domain model
guidelines.md    the engineering standard this was built under
```

## How this was built

Every slice was written against a written domain model, mechanically triaged, then audited by an
independent reviewer with its own context that saw the code and the rules but never the author's
justification. The standard is in [`guidelines.md`](guidelines.md), and what it forbids is listed
there before what it requires.

Eight slice audits returned clean. A final audit over the whole codebase then returned
**violations** — and the worst of them was a defect no per-slice review could have seen: two phases
read evidence through accessors that bypassed the memory gate, so the `none` control arm leaked the
state it is defined by withholding. It survived eight rounds because the test asserted on the
briefing header instead of on the prompt actually sent.

That audit also found long-term memory leaking between invocations (the harmful-retrieval rate moved
14% → 29% on a second run of the same command), a metric labelled as a step count while indexing the
assessment series, the Admiralty grading *reason* generated and then thrown away, and the narrative
half of the report carrying no grounding check. Every one is fixed, each with a regression test in
[`tests/test_regressions.py`](tests/test_regressions.py) that names the defect it prevents.

The gate then failed **twice more**, and both rounds are worth recording.

The second round caught me removing the render-time citation check — I had accepted a static claim
that it was redundant without testing it, and an auditor broke it in three lines, because pydantic
does not revalidate on mutation into a held dict. The third caught a bug I had introduced *with my
own fix*: gating hypotheses by memory mode meant one could never be examined, and since ACH ranks by
least-disconfirmed, an unexamined hypothesis scoring zero beat every hypothesis actually tested. A
guess nobody checked would have led the report. That one is fixed at the root — scoring zero by
never being examined is not the same as surviving examination — and untested hypotheses are now
shown and marked rather than hidden.

Those rounds are recorded rather than quietly repaired because they are the most useful evidence
here. Three things this process demonstrated that a clean run could not: a per-slice review cannot
see a property violated only by the interaction between slices; a test that asserts on an
intermediate will pass while the property it names is false; and a fix is a change like any other,
so it needs auditing too. I was wrong about the citation guarantee twice, and both corrections are
in the code with a test naming the defect.
