# Agentic Harness for OSINT

Here's what I built: an investigator that takes a company, a person, or a claim someone made, and
actually goes and checks it. It searches, reads real sources, weighs what it finds against competing
explanations, and hands back a report with citations and a confidence number that means something.
Alongside it sits a benchmark and an instrumented harness, because I wanted proof this works, not a
demo that only looks good once.

The agent itself is an explicit state machine. Nothing hidden inside a black box. Every transition
gets recorded, and every number in the evaluation gets recomputed from those records instead of
typed in from memory. If I can't point at the log a number came from, it doesn't go in this document.

---

## Run it in two minutes, no API key needed

The whole benchmark replays from a recorded cassette. Anyone reviewing this can run all of it
offline, no key, no waiting on a network.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; use .venv/bin/activate elsewhere
pip install -e ".[dev]"

osint-harness cases                                  # the 14 benchmark subjects and claims
osint-harness investigate ada-lovelace-person        # one investigation, replayed offline
osint-harness ablate                                 # all 14 cases x 3 memory modes, with a report
pytest && mypy && ruff check .                       # 247 tests, strict types, clean lint
```

Reports land in `runs/<case>-<memory>/findings.md`, and the ablation report in
`runs/results/ablation.md`.

**One thing to be upfront about: what you're running offline is the machinery, not the analysis.**
Without an API key, the harness falls back to a rehearsed analyst. It does real retrieval, produces
real citations, but it doesn't reason, so it abstains on every single case. I did that on purpose,
and I explain why down in [Honest status](#honest-status-whats-actually-verified-and-what-isnt).
Want the real thing instead?

```bash
export OPENROUTER_API_KEY=...
osint-harness --live --record ablate
```

---

## Or just watch it run, live, no setup at all

I put a hosted version up too: [frontend](https://agentic-osint-harness-kritantasasanroys-projects.vercel.app),
backed by a [FastAPI service](https://osint-harness-api.onrender.com/docs) running the actual
package (`web/backend/`, `web/frontend/`, separate from the graded submission itself).

Every investigation you start there is real. A live model, live search, live page retrieval, the
whole six-phase loop, while you watch a phase rail move through it. There's no rehearsed stand-in
anywhere on that path, and that's not just a setting I left on, the request type it accepts has no
field to ask for anything else, and the one place a fallback used to live got deleted along with it.
Can't run it live right now (no key configured, or the shared daily allowance is spent) means the
request gets refused outright, with the reason stated plainly, never quietly satisfied some other
way. A run takes a few minutes and about a dozen model calls, shared across whoever's visiting that
day, so don't be surprised if it says the allowance is gone, that's the real free tier, not a demo
limit I invented.

One button on that page stays offline on purpose: the memory ablation, comparing `none`/`short`/`long`
across all 14 cases. Making that live would actually make it worse, not more honest, since the whole
comparison only means something if every arm sees identical retrieval. A live run would measure the
web changing between arms instead of memory doing anything, which is exactly the mistake decision #3
below explains. It replays the same recorded pages the CLI's own published numbers are audited
against, so what you see there is real data, just deliberately not live data.

---

## How it works

Six phases, named after the stages of the intelligence cycle, wired together as a state machine:

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

Full diagram and data flow live in [`docs/architecture.md`](docs/architecture.md). Design reasoning,
findings, and limitations are in [`docs/report.md`](docs/report.md). The domain model I audited the
code against is in [`docs/design/domain-model.md`](docs/design/domain-model.md).

---

## Decisions the brief left open

The brief was clear about one thing: it cared more about my choices than whether I followed the spec
word for word. So here's what I actually decided, and why. Just the calls that mattered.

### 1. I borrowed the scoring from real intelligence tradecraft instead of inventing my own

I didn't want to invent a confidence number out of thin air. Instead the harness leans on three
instruments that already exist, tested well outside this project, that anyone reviewing this can
check against a real external standard.

- **The Admiralty (NATO) system.** Grades evidence on two separate axes: source reliability (A
  through F, about the publisher) and information credibility (1 through 6, about the specific
  claim). Mixing the two up is the single most common way people misuse this scale, so I kept them
  on different objects entirely. No way to accidentally conflate them.
- **Analysis of Competing Hypotheses (ACH).** Resolves contradictions by ranking hypotheses by which
  one is *least disconfirmed*, not which has the most support. Evidence consistent with five
  different explanations is weak evidence. Evidence that rules one out is strong. This is the part
  that actually makes the agent resistant to confirmation bias, structurally, not just because I
  told it to be careful.
- **ICD 203 words of estimative probability.** Turns confidence into a named band with an actual
  numeric range attached, so it can be Brier-scored later instead of just admired and forgotten.

One nice side effect: every investigation, company, person, or claim, runs through the same single
mechanism. The kind of subject only changes the opening questions. It never branches the underlying
logic.

### 2. No orchestration framework, no vector database

The whole state machine is about a hundred lines. Every metric the brief wants is just a hook off its
loop: one `Step` recorded per transition, carrying the phase, why it moved, what it called, what it
spent. Pull in a framework and I'd have spent my time instrumenting somebody else's abstraction just
to claw back numbers the hand-written version already gives me for free. Worse, it would've hidden
the exact part of this project a reviewer actually wants to look at.

### 3. Record and replay cassettes aren't a convenience, they're load-bearing

The memory ablation compares `none`, `short`, and `long` memory modes. That comparison only means
anything if all three see *identical* evidence. So every external lookup gets keyed and recorded,
and replay is the default. A cassette miss is an error, not a quiet fallback to the live network,
because a silent live call would destroy the one guarantee the cassette exists to give you. Measure
a delta against live search and you're not measuring memory anymore. You're measuring the weather.

Nice side benefit too: the entire benchmark runs with zero API key, which is exactly what let you
run it above in two minutes.

### 4. Memory recall is lexical on purpose, the weakness *is* the point of the experiment

Long-term memory finds prior investigations by lexical similarity on the subject description, not by
embeddings. Here's the thing: an embedding retriever smart enough to never confuse two people who
share a name would leave the harmful-retrieval metric with nothing to actually catch. And same-name
confusion is exactly how memory tends to fail in real OSINT work. So I pinned that confusion in place
with a test, and sure enough, the measured ablation shows memory interference showing up only in
`long` mode. Not a bug I missed. That's the experiment working.

### 5. Memory can never become evidence, and that's enforced structurally

The archive only stores what an episode *concluded*, never what it actually read. Anything recalled
lands in a briefing explicitly labelled as unverified recall, nothing more.

But labelling alone isn't the guarantee, and neither is the archive's shape by itself. The real
guarantee is one enforced rule: `Evidence` has exactly one construction site, and `record_evidence`
refuses anything whose URL isn't already among the documents this episode actually retrieved. An
`Investigation` also refuses to be built or reloaded while holding one that breaks that rule. I know
this rule earns its keep because an independent auditor broke an earlier version of this exact claim.
The archive's shape alone wasn't enough, since a record could still get assembled outside the guarded
path. The validator exists because that failure was real, not hypothetical.

### 6. Ground truth can never reach a prompt, also structural

`Investigator.investigate()` only accepts a `Subject`. No overload anywhere takes a `BenchmarkCase`,
so the object holding the expected answer never crosses into the agent's world at all. I wrote a test
that runs a real case end to end and checks that none of its expected findings, notes, or trap labels
show up in any prompt the model actually saw.

### 7. No fallback routing across models, even though OpenRouter offers it

OpenRouter can quietly re-route a failed or declined request to a completely different model behind
the same call. I turned that off on purpose. Substituting a different model partway through a
benchmark run would put episodes produced by two different models into the same comparison, the same
mistake as comparing memory modes against different evidence. So a failure just raises, the episode
gets tagged, and it stays in the denominator instead of quietly disappearing. For a product, that
tradeoff usually goes the other way. For a measurement, it can't.

### 8. Reward gets decomposed, version-stamped, and computed offline

A single scalar reward can't answer "where exactly did this go wrong", and that's the actual question
the brief is asking. So I broke it apart:

| Component | Weight | Why |
| --- | --- | --- |
| Correctness | 0.40 | Being right matters most, plainly. |
| Calibration | 0.25 | Half Brier score, half "was this confidence even defensible". Brier alone gets gamed by hedging everything to even odds. |
| Evidence quality | 0.20 | Mean Admiralty weight, discounted if everything came from one publisher. |
| Citation integrity | 0.15 | A gate, not a gradient. One fabricated citation zeroes it out completely. |
| Efficiency | capped penalty | So a cheap wrong answer still can't win. |

Reward gets computed after an episode ends, straight from the stored trajectory. Nothing the agent
can reach ever imports the scoring code, so it has no way to chase its own score. The formula itself
carries a version stamp too. Change the formula, bump the version, and every number computed under
the old one gets invalidated automatically.

### 9. Abstaining scores zero in both directions

Abstaining when a real verdict was available, and committing to an answer when abstention was the
only honest move, both score zero on correctness. No partial credit either way, while a
supported-versus-partially-supported miss still gets half credit. The episode can still pick up
points elsewhere, and that's intentional: an agent that landed on the wrong verdict but cited real,
well-graded sources did something genuinely better than one that just made citations up. Correctness
carries the biggest weight, so getting it wrong still dominates the score.

One related call: `INSUFFICIENT_EVIDENCE` is a judgment, not a hypothesis. A refusal to commit makes
no actual claim, so it can't be disconfirmed by anything. Letting it sit in the ACH matrix would let
"I don't know" quietly rack up consistency points against evidence it never engaged with.

### 10. OpenRouter over one vendor's API, and why this specific model

`ModelClient` genuinely only ever needed one thing from a provider: turn a prompt into valid,
structured JSON. Nothing about the design depends on a specific lab, so I run the harness against
OpenRouter instead, which sits in front of a lot of providers, including some priced at literally
zero, all behind one API and one key.

Worth being upfront about the catch here: the free tier is rate-limited, not metered. 20 requests a
minute, 50 a day if you've never bought credits, 1,000 a day once you've put in a one-time $10. And
it's enforced per account, not per key. `LiveModel` paces every call to stay under the per-minute
ceiling, but the daily ceiling is a hard wall I can't lift from code. That's exactly why the verified
live results further down are a small, deliberate run, not the full 42-episode sweep.

I picked the specific free model by checking OpenRouter's actual model listing directly, rather than
trusting blog rankings. Good thing too: that research turned up a pile of programmatic SEO content
confidently naming models that don't even appear in the real catalogue. OpenRouter says outright that
the free lineup rotates without notice, free today, paid tomorrow, which is exactly why the model id
is a constructor argument and a `--model` flag, never a hardcoded string.

That flag earned its keep faster than I expected. My first pick was `nex-agi/nex-n2.5-pro:free`, and
running it live taught me something no mock ever could: it burns the large majority of its token
budget on hidden reasoning before writing an answer (I watched it spend 319 of 397 completion tokens
on a genuinely simple prompt). I capped reasoning effort and raised the output ceiling, which made
short prompts work, and I thought that was the end of it.

It wasn't. On the heavier prompts further into an investigation, the ones carrying a full briefing of
leads and hypotheses, that model still spent its entire budget thinking and returned nothing, after
sitting there for close to six minutes. Direction would succeed and Collection would die. So I went
back to the catalogue and actually measured candidates against the real Collection schema instead of
picking on reputation: `nvidia/nemotron-3-super-120b-a12b:free` answered the same prompt correctly in
**3.9 seconds** where the old one had burned 350 and failed. That is now the default.

The lesson I'd keep: "free model that supports JSON output" is not a specification. Two models with
identical capability flags in the same catalogue differed by two orders of magnitude on the workload
that actually mattered, and the only way to find that out was to run the real prompts through both.
The failure is at least diagnosed by name now ("the model spent its budget on reasoning, raise
max_tokens or lower reasoning effort") rather than a bare "no reply", which is what let me tell a bad
model apart from a bug in my own code.

### 11. Search became its own tool, not something the model does for me

No provider hands out server-side web search for free. Rather than build a special case into the
live path around that gap, I pulled search entirely out of `ModelClient` and turned it into its own
`Tool`, called `WebSearch`, using DuckDuckGo's keyless HTML front end, sitting right next to
`Encyclopedia` and `PageFetch`.

This turned out to be a real improvement, not a workaround. Because it's now a `Tool`, a search gets
cassette-recorded and replayed offline exactly like every other retrieval, which was never true
before (the old model-driven search was either a live call to a provider or a scripted stand-in,
nothing in between). `ModelClient` is down to exactly one method now, `decide()`, which is honestly
just a cleaner statement of what a reasoning provider actually owes this harness in the first place.
Scraping DuckDuckGo's HTML is fragile to markup changes, and that's by design of the approach, not an
oversight on my part. I marked it with this project's own `ponytail:` convention: a deliberate
ceiling with a named upgrade path, a paid search API, whenever reliability actually becomes the
bottleneck.

---

## The benchmark

14 cases: 5 companies, 4 people, 5 claims. I picked them to be adversarial, not just varied for
variety's sake. Every judgment type and every trap gets exercised at least once, and a test fails the
moment that stops being true.

| Case | Trap | Why it's there |
| --- | --- | --- |
| Vantage Nebula Holdings | insufficient evidence | An invented company. Any confident verdict here is fabrication, full stop. |
| John Smith (no qualifiers) | same name | No identifiable subject at all. Abstaining is the only honest answer. |
| Michael Jordan (ML researcher) | disambiguation | A resolvable identity hiding behind a famous namesake, where abstaining would actually be *wrong*. |
| The current King of France is bald | false premise | Neither true nor false as stated. |
| Einstein's Nobel "for relativity" | popular myth | Half true and genuinely misleading. Forces the partially-supported verdict to actually be reachable. |
| Great Wall visible from space | popular myth | A myth with more sources behind it than its own correction. Tests reliability over raw volume. |
| Theranos, Wirecard | adverse media | Findings the subject itself actively denied. |
| Apollo 11 landed in 1969 | none | A deliberate floor. An investigator that won't commit here isn't being careful, it's just miscalibrated. |

---

## Honest status: what's actually verified and what isn't

**Verified, with commands you can run yourself:** 247 tests pass, `mypy --strict` comes back clean
across 40 source files, `ruff check` is clean, and a full 42-episode ablation (14 cases times 3
memory modes) runs end to end offline and writes its report. Running `ablate` twice gives
byte-identical output both times, which is the only reason the numbers below are worth quoting at
all.

**The deterministic sweep below still uses the rehearsed analyst**, not the live model, and that's a
deliberate choice, not a gap I'm hiding. The free tier's daily cap (50 calls a day, no credits
purchased) can't support a 42-episode live sweep in one sitting, that's 200-plus calls, and a
benchmark run that dies halfway through would be worse than one that's honestly labelled as offline.
The rehearsed analyst abstains on literally every case, so it's only correct on the three where
abstaining happens to be the right call. That's 3 out of 14, or 21%. What this validates is the
machinery, not the agent:

| Memory | Accuracy | Mean reward | Irrelevant retrieval | Harmful retrieval | Memory-interference failures |
| --- | --- | --- | --- | --- | --- |
| `none` | 21% | 0.332 | 0% | 0% | 0 |
| `short` | 21% | 0.332 | 0% | 0% | 0 |
| `long` | 21% | 0.332 | 14% | 14% | 2 |

The one column that's actually telling you something is the last one. `long` mode shows memory
interference that `none` and `short` simply don't, on similarly named subjects, caught and attributed
automatically. The instrumentation works even when the analyst behind it doesn't.

**The live path itself is verified now too, on a real case, against a real free model, not
simulated.** I ran `--live --record` once on `ada-lovelace-person` and it completed five phases in
full: Direction, Collection, Appraisal, Reconciliation, and Reflection, which then looped back into a
second round of Collection, before that sixth call got truncated by the model's own token ceiling.
Everything up to that point is genuinely real: real search, real Wikipedia retrieval, 18 graded
assertions pulled from actual page text, 72 ACH evidence-to-hypothesis judgments, and an honest
`supported` verdict at 0.78 probability with specific reasoning about which hypothesis survived which
disconfirming fact. I kept the full transcript at [`docs/example-live-run/`](docs/example-live-run/),
exactly as it came out, not cleaned up for presentation.

Running it live surfaced two things no mock could have shown me:
- This model spends most of its token budget on hidden reasoning before it answers, and an
  unremarkable prompt blew through a first, tighter token ceiling before writing anything at all.
  Fixed by capping reasoning effort and raising the ceiling (decision #10, above).
- Appraisal produced genuinely well-reasoned Admiralty judgments, but wrote the domain field as
  `"en.wikipedia.org, biographical, technical, commemorative..."` instead of a bare hostname. Since
  evidence always gets keyed by the clean domain alone, every one of those gradings landed under a
  key nothing ever looked up. Real analytic work, thrown away silently. A live-only bug no scripted
  test could ever produce, since neither `ScriptedModel` nor `RehearsedModel` ever writes free text
  into a structured field. I fixed it at the point of use, in `Source.domain_only`, with a
  regression test behind it.

The run still ended in a halt, on that second Collection call going past even the raised ceiling.
The crash-tolerant halt handled it exactly the way I designed it to: a complete, honestly-labelled
dossier instead of a crash. Want to reproduce or extend it? One command and a key:
`osint-harness --live --record investigate <case-id>`. A full live ablation is one command too
(`osint-harness --live --record ablate`), it just needs either a paid OpenRouter balance or a few
days spread out to clear the free-tier daily cap.

---

## Repository layout

```
src/osint_harness/
  domain/        subjects, provenance and Admiralty grading, evidence and ACH, the investigation
  graph/         the state machine, the six phases, the memory-gated briefing, reply schemas
  sources/       encyclopedia, page fetch and keyless web search, all behind the record/replay cassette
  model/         the reasoning client, OpenRouter-backed when live, or scripted for tests
  memory/        the cross-episode archive and the publisher register
  bench/         benchmark cases, reward, failure taxonomy, run aggregation
  report/        the analyst-facing dossier and the ablation report
benchmark/       the 14 cases, and the recorded cassette they replay from
docs/            architecture, design rationale, domain model
guidelines.md    the engineering standard this was built under
```

## How I actually built this

Every slice went in against a written domain model first, got triaged mechanically, then got audited
by an independent reviewer running in its own context, one that saw the code and the rules but never
my reasoning for why I thought it was fine. The full standard lives in
[`guidelines.md`](guidelines.md), and what it forbids is listed before what it requires. That
ordering was deliberate too.

Eight slice audits came back clean. Then a final audit over the whole codebase came back with
violations, and the worst one was a defect no per-slice review could ever have caught on its own: two
phases were reading evidence through accessors that quietly bypassed the memory gate, so the `none`
control arm was leaking exactly the state it's supposed to be defined by withholding. It survived
eight rounds of review because the test was asserting against the briefing header instead of the
actual prompt sent to the model.

That same audit also caught long-term memory leaking between separate invocations (the
harmful-retrieval rate jumped from 14% to 29% on a second run of the identical command), a metric
labelled as a step count while actually indexing the assessment series, the Admiralty grading's
*reason* field being generated and then just thrown away, and the narrative half of the report
carrying no grounding check whatsoever. Every one of those is fixed now, each with its own regression
test in [`tests/test_regressions.py`](tests/test_regressions.py) that names exactly the defect it's
guarding against.

Then the gate failed two more times after that, and both rounds are worth writing down rather than
quietly fixing and moving on.

Round two caught me removing the render-time citation check. I'd accepted a static claim that it was
redundant without actually testing that claim, and an auditor broke it in three lines, because
pydantic just doesn't revalidate on mutation into a held dict. Round three caught a bug I'd
introduced with my own fix from round two: gating hypotheses by memory mode meant one could end up
never examined at all, and since ACH ranks by least-disconfirmed, an unexamined hypothesis scoring
zero disconfirmations would beat every hypothesis that was actually tested and survived. A guess
nobody ever checked would've won the report. I fixed that one at the root: scoring zero because you
were never examined isn't the same thing as surviving examination, and untested hypotheses now get
shown and labelled instead of hidden.

I'm recording these rounds instead of quietly patching them and moving on because they're honestly
the most useful evidence in this whole document. Three things this process proved that a single
clean run never could have: a per-slice review can't see a property that only breaks from the
interaction between slices, a test that asserts on an intermediate value will happily pass while the
actual property it's supposed to guard is false, and a fix is a change like any other, so it needs
its own audit too. I was wrong about the citation guarantee twice in a row, and both mistakes are
sitting right there in the code, each with a test that names exactly what went wrong.
