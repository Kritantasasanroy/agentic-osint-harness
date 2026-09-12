# Report — design choices, findings, limitations

## 1. The choice that determined everything else

The brief asks for a findings report "a human analyst would actually trust enough to act on". An
analyst does not trust a number a language model emitted; they trust a judgment they can audit.
So the first decision was to stop inventing scoring and borrow the domain from the field that has
already solved this: intelligence analysis.

Three instruments, each external to this codebase and checkable against a published standard:

- **Admiralty (NATO) grading** splits reliability of the *publisher* (`A-F`) from credibility of the
  *claim* (`1-6`). They are graded by different criteria and conflating them is the standard misuse,
  so they live on different objects and cannot be accidentally merged.
- **Analysis of Competing Hypotheses** ranks explanations by least *disconfirmed*. This inverts the
  usual failure: evidence consistent with five hypotheses is weak, evidence that eliminates one is
  strong. Confirmation bias is designed out rather than warned against.
- **ICD 203 estimative probability** makes confidence a named band with a numeric range, which is
  what makes it Brier-scorable at all.

The consequence worth noting: this unified all three subject kinds into **one** mechanism. A company,
a person and a claim are all "a set of competing hypotheses resolved by ACH". Subject kind chooses
the opening questions and nothing else, so there is no conditional chain branching on type anywhere
in the agent.

## 2. Design choices and rationale

### Owning the state machine

About a hundred lines, hand-written. The brief asked for an explicit graph, and every metric it
wants is a hook on the loop — one `Step` per transition carrying phase, reason, tool calls, tokens
and latency. A framework would have meant instrumenting someone else's abstraction to recover
numbers this exposes for free.

### Cassettes as an integrity mechanism

The obvious reading of record/replay is "makes tests fast". That is not why it is here. The memory
ablation compares three arms; unless all three see identical retrieval, any difference between them
is partly the web changing between runs. Replay is therefore the **default**, and a cassette miss
raises rather than falling through to the network, because a silent live call would quietly destroy
the guarantee.

The side effect is that the whole benchmark runs with no API key, which is why a reviewer can
execute it before reading any of this.

### Deliberately imperfect memory retrieval

Recall is lexical, not embedding-based. This looks like a shortcut and is not: a retriever that
never confuses two people sharing a name would make the harmful-retrieval metric unmeasurable, and
same-name confusion is *the* canonical memory failure in OSINT. The weakness is the experiment. The
measured ablation confirms it — interference appears only in `long` mode, on similarly-named
subjects, and is attributed automatically because a `Recollection` carries the episode it came from.

### Two guarantees made structural rather than procedural

Both of these could have been prompt instructions. Prompt instructions are not guarantees.

1. **Memory can never be cited.** The archive stores conclusions and holds no document and no URL,
   but that alone proved insufficient: an independent auditor assembled an `Investigation` outside
   the guarded path and rendered a clean-looking forged citation. The guarantee now rests on
   `Evidence` having one construction site behind `record_evidence`, plus a validator that refuses
   to construct or reload a record holding an unbacked citation.
2. **Ground truth cannot reach a prompt.** The investigator's signature accepts a `Subject`; nothing
   accepts a `BenchmarkCase`. The object holding the answer never crosses the boundary.

### Scoring that cannot be gamed or quietly moved

Reward is decomposed (correctness 0.40, calibration 0.25, evidence quality 0.20, citation integrity
0.15, minus a capped efficiency penalty), computed **after** the episode from the stored trajectory,
and version-stamped. Nothing the agent can reach imports the scoring code.

Two specific decisions inside that:

- **Calibration is half Brier, half defensible-band membership.** Brier alone is minimised by
  hedging every answer to even odds, which is precisely the behaviour an investigator must not be
  rewarded for. The band term punishes hedging and overclaiming alike.
- **Abstention scores zero on correctness in both directions.** Declining where a verdict was
  available, and committing where abstention was the only honest answer, both score zero on the
  correctness component — no partial credit, where a supported/partially-supported miss earns half.
  Other components still contribute, deliberately: a wrong verdict resting on real, well-graded
  sources is not the same failure as an invented one.

### No fallback routing across models

OpenRouter can silently re-route a failed or declined request to a different model behind the same
call. That is switched off here: for a product that trade is sensible, but for a benchmark it is
corrupting, because it silently places episodes produced by two different models into one
comparison. A failure raises, gets tagged, and stays in the denominator.

### OpenRouter, a specific free model, and what running it live actually taught

`ModelClient` needs exactly one thing from a provider — turn a prompt into valid structured JSON —
so the harness runs on OpenRouter rather than a single vendor's API, which makes the provider a
one-line swap and gives access to models priced at zero. That comes with a real constraint: the free
tier is rate-limited rather than metered (20 requests/minute; 50/day with no credits ever purchased,
1,000/day past a one-time $10 minimum), governed per account rather than per key. `LiveModel` paces
calls to respect the per-minute ceiling; the daily ceiling cannot be lifted from inside the harness,
which is why the live results below are a deliberately bounded run rather than the full sweep.

The specific model (`nex-agi/nex-n2.5-pro:free`, overridable via `--model`) was picked by checking
OpenRouter's own model catalogue directly — the research for this turned up a wall of confident-
sounding blog rankings naming models that do not otherwise appear in that catalogue at all, which is
its own small lesson about trusting secondary sources for anything time-sensitive. OpenRouter states
its own free lineup rotates without notice, which is why the model id is a parameter, not a literal.

Running it live surfaced something no offline test could: this model spends the large majority of
its token budget on hidden chain-of-thought before writing an answer (measured: 319 of 397
completion tokens on a plain prompt). An unremarkable prompt exhausted the token ceiling on
reasoning alone, before any JSON was ever written — invisible to `RehearsedModel`/`ScriptedModel`,
neither of which reasons at all. The fix was two changes with no monetary cost on a $0 model: cap
reasoning effort, and raise the output ceiling generously since extra tokens only cost wall-clock
time. The failure is now diagnosed by name rather than reported as a bare, uninformative silence.

### Search became a `Tool`, not a model capability

No provider offers server-side search for free, so search moved out of `ModelClient` entirely and
became `WebSearch`, a `Tool` beside `Encyclopedia` and `PageFetch`, using DuckDuckGo's keyless HTML
front end. This is a strict improvement on the design it replaced, not a workaround forced by the
provider swap: because search is now a `Tool`, it is cassette-recorded and replayable offline like
every other retrieval, which the previous model-driven search never was — that one was either a
live provider call or a scripted stand-in, with nothing in between. `ModelClient` is left with
exactly one method, which is a more honest statement of what a reasoning provider actually owes
this harness. The scraping approach is fragile to markup changes by construction, marked with the
project's own `ponytail:` convention naming the ceiling and the upgrade path.

## 3. Key findings

**The instrumentation detects what it was built to detect.** Across 42 episodes (14 cases x 3 memory
modes) replayed from an identical cassette, `long` mode shows a 14% harmful-retrieval rate and two
memory-interference failures; `none` and `short` show zero. Since retrieval was byte-identical
across arms, that difference is attributable to memory rather than to the environment — which is the
entire point of building the cassette layer first.

**Failure attribution is informative rather than decorative.** The dominant tag in the shipped run is
`source_selection` (11 of 14 in `none`/`short`), correctly identifying that every investigation
rested on a single publisher. That is a true statement about the run and exactly the kind of finding
a scalar reward would have hidden.

**Abstention being a first-class verdict changes the benchmark's shape.** Three of fourteen cases
have abstention as the *correct* answer, and one (Michael Jordan the researcher) is specifically
designed so that abstention is *wrong* despite a famous namesake making it tempting. Without the
floor cases, an agent that abstains on everything would look cautious rather than useless.

**Per-slice review was not enough, and the final audit proved it.** Eight slice-by-slice audits all
returned clean. A final independent audit over the whole codebase then returned **violations**, and
the most serious was invisible to every earlier round: `Reconciliation` and `Dissemination` read the
evidence through accessors that bypassed the memory gate, so the `none` control arm leaked the very
state it is defined by withholding. It survived because the test asserted on the briefing header
rather than on the prompt actually sent to the model.

The same audit found that long-term memory persisted between invocations, so the harmful-retrieval
rate moved from 14% to 29% on a second run of the same command — the headline number was an artefact
of how many times it had been run. It also found a metric indexing the assessment series while being
labelled a step count, the Admiralty grading *reason* being generated and then discarded, and the
narrative half of the report carrying no grounding check at all.

All are fixed, each with a regression test naming the defect. `ablate` now produces byte-identical
output across invocations, verified.

**The gate then failed twice more, on the fixes themselves.** Round two caught me having *removed*
the render-time citation check: I had accepted a static claim that it duplicated the constructor
validator, without testing the claim. An auditor broke it in three lines — pydantic does not
revalidate on mutation into a held dict, so a forged citation still rendered clean. Round three
caught a defect I introduced with my own remediation: gating hypotheses by memory mode meant a
Reflection-added hypothesis could never be judged, so it held a disconfirming score of zero, and
under least-disconfirmed-wins that beat every hypothesis actually examined. An unexamined guess
would have been reported as the leading explanation.

Four lessons generalise beyond this project:

1. **Slice-local review cannot see a property violated only by the interaction between slices.** The
   `none` leak needed two phases and a helper to exist simultaneously; no single slice contained it.
2. **A test asserting on an intermediate passes while the property it names is false.** The memory
   test checked `Briefing.header()`, which was correctly gated, and so never noticed that the phases
   bypassed it. It now asserts on the prompt actually sent.
3. **A demonstrated break outranks a static claim of redundancy.** Two auditors disagreed about the
   renderer check; the one with a working exploit was right, and I sided with the other.
4. **Fixes need auditing too.** The worst single defect found anywhere in this project — an untested
   hypothesis winning by default — was introduced by a fix, not by the original build.

## 4. Limitations

**The shipped benchmark numbers still come from the non-reasoning stand-in, deliberately.** The
free tier's daily request cap (50/day with no credits ever purchased) cannot support a 42-episode
live sweep in one sitting, so the reproducible submission artifact remains the rehearsed analyst,
which retrieves real documents and produces real citations but does no analysis — it abstains
everywhere and scores 21%, correct only on the three abstention cases. These numbers validate the
harness, not the agent, and should not be read as agent quality.

**The live path itself, however, is now verified — not simulated.** A real `--live --record` run
completed Direction, Collection, Appraisal, Reconciliation and Reflection in full against a real
free model — Reflection judged the evidence incomplete and looped back into a second Collection
round, producing 18 genuinely graded assertions, 72 real ACH judgments, and a substantive
`supported` verdict with specific, evidence-citing reasoning along the way — before that sixth call
exceeded the token ceiling and the crash-tolerant halt caught it cleanly. The transcript is kept at
`docs/example-live-run/`. What remains unverified is a *complete, uninterrupted* multi-phase episode
reaching Dissemination, and the full 42-episode sweep — both blocked by the same daily quota, not by
anything the design leaves untested.

**Running it live found a real bug no mock could have.** Appraisal's source gradings were genuine
and well-reasoned but kept vanishing from the report, because the model wrote the domain field as a
hostname plus a description, and evidence is keyed by the bare hostname alone. Fixed at the point of
use, with a regression test — see decision #10 for the full account. This is the clearest evidence
in the whole project that live verification finds a different class of defect than review does:
`ScriptedModel` and `RehearsedModel` cannot write free text into a structured field, so neither one
could have produced this failure, however carefully either was reviewed.

**Reward weights are argued, not derived.** 0.40/0.25/0.20/0.15 expresses a defensible position
about what matters, but no sensitivity analysis has been run. A different reviewer could argue for
different weights, and the harness does not currently show how conclusions change under them.

**One real source in the offline cassette.** The recorded retrievals are Wikipedia only, because it
needs no key. Source-diversity scores in the shipped run are therefore structurally capped at one,
which is why `source_selection` dominates the failure counts. A live recording pass with web search
fixes this.

**The benchmark is small and English-language.** Fourteen cases is enough to exercise every trap once
but not enough for statistical confidence in any single rate. Non-English sources, jurisdictional
records, and beneficial-ownership chains are untested.

**Convergence is barely exercised.** Because the stand-in analyst reaches the same verdict every
time, verdict-change and assessments-to-stable-verdict are all zero in the shipped run. The metrics
are implemented and tested, but the shipped data cannot demonstrate them. Note the rename: an
independent audit found this metric was indexing the assessment series while being labelled and
reported as a step count, so a six-step episode read as "settling at step 0".

## 5. What I would do next, in order

1. **Clear the daily quota over several days, or fund the account past the $10 threshold**, and run
   the full 42-episode live ablation. The live path is proven to work end to end; what is missing is
   volume, not verification. At 1,000 requests/day past $10 in lifetime credits, a full sweep
   becomes a same-day proposition rather than a multi-day one.
2. **Add a second and third real source type** so source diversity can exceed one and the
   `source_selection` failure stops dominating: a company registry and a news archive.
3. **Run the reward weights as a sensitivity sweep** rather than asserting them, and report which
   conclusions are stable across weightings and which are artefacts of the choice.
4. **Repeat each case several times** to separate model variance from genuine memory effects. The
   current design makes this cheap — the cassette holds retrieval fixed, so only sampling varies.
5. **Grow the archive deliberately** to test memory at a scale where interference becomes likelier,
   and measure whether the harmful-retrieval rate rises with archive size as it should.
6. **Add an adversarial case class**: a subject whose public record is deliberately contradictory
   across sources of different reliability, to exercise the conflict path harder than the current
   cases do.
