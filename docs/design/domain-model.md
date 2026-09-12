# Domain Model — OSINT Agentic Harness

Written before implementation, per `guidelines.md` Part 2 step 2. The implementation is audited
against this document; where the code and this file disagree, one of them is a defect.

---

## 0. The framing decision

The domain here is **intelligence analysis**, a field with a century of settled vocabulary. Rather
than invent names, this model borrows the profession's nouns and its three standard instruments:

| Instrument | What it gives us | Requirement it satisfies |
| --- | --- | --- |
| **The intelligence cycle** (Direction → Collection → Processing → Analysis → Dissemination) | The phases of the state machine, and their names | "planning and adapting its investigation" |
| **The Admiralty (NATO) grading system** — source reliability A–F × information credibility 1–6 | A defensible, non-arbitrary evidence rubric | "evaluating evidence and source reliability" |
| **Analysis of Competing Hypotheses (ACH)** | A mechanism that resolves contradictions by *disconfirmation* rather than by vote | "handling conflicting or insufficient information appropriately" |
| **ICD 203 words of estimative probability** | Confidence as a named band with a numeric range, so it can be scored for calibration | "structured findings report with … confidence" |

This is the single most consequential choice in the project, because it makes the "definition before
name" test trivial to pass: every concept below is defined by what it is to an analyst, independent
of this codebase.

**The unifying decision:** every investigation — company, person, or claim — is modelled as a set of
**competing hypotheses** about the subject, resolved by ACH. A claim investigation's hypotheses are
the obvious ones; a company or person investigation's hypotheses are generated during Direction
("the subject is an operating entity with no adverse findings" vs. stated alternatives). This gives
one mechanism instead of three, and it removes what would otherwise be a conditional chain
branching on subject kind — the subject kind instead selects *seed leads* polymorphically.

---

## 1. Concepts

Each is marked **general** (any product doing this work has it), **ours** (specific to how this
harness works), or **plumbing** (no business meaning).

### `Subject` — *general*, abstract
> The entity or proposition an investigation is about.

Abstract base. Owns identity (a display name, and disambiguating qualifiers such as a jurisdiction
or a date). Its one piece of polymorphic behaviour is `seed_leads()`, which returns the opening
lines of enquiry for that kind of subject — this is where the three subject kinds differ, and the
only place they do.

Subtypes — `Company`, `Person`, `Claim` — are genuine *kinds*, not states: "company" can never
become false for a given subject, so they pass the qualifier test and are not adjective-on-noun
violations.

- `Company`: adds jurisdiction and any known registration identifier.
- `Person`: adds context qualifiers (affiliation, role, locale). Carries the disambiguation burden —
  this is the subject kind where same-name collision is the dominant failure mode.
- `Claim`: adds the proposition text and, where stated, the date the proposition is asserted about.

**States:** none. A subject is immutable input.
**Invariants:** a display name is non-empty; a `Claim`'s proposition is non-empty.
**CRUD:** created once from benchmark data or CLI input, read-only thereafter. No update, no delete.

### `Source` — *general*, own transaction unit
> A publisher of information, existing independently of any one investigation.

Identified by its registrable domain. Carries an Admiralty **reliability grade** (A–F) with the
reason it was graded that way, and the count of investigations it has appeared in.

`Source` is the **second aggregate root**, deliberately separate from `Investigation`, because
reliability accumulates *across* episodes — it is one of the two things long-term memory actually
remembers. Evidence therefore references a source **by domain**, never by held object.

**States:** the reliability grade itself (A–F), plus `UNRATED` for a domain seen for the first time.
**Invariants:** a grade change records its reason; the grade is never silently overwritten.
**CRUD:** create on first sighting; update only the grade and the appearance count, both with a
recorded reason; never deleted (evidence points at it).

### `Document` — *general*
> A specific artifact retrieved from a source at a specific time.

URL, title, the retrieved text, the source domain it came from, and the retrieval timestamp. Several
pieces of evidence can be extracted from one document, which is why this is a concept and not four
fields on `Evidence`.

**States:** none — a document is a fact about what was retrieved.
**Invariants:** a document exists only if it was actually retrieved during this investigation. This
is the anti-fabrication invariant, and the citation check at Dissemination enforces it.
**CRUD:** append-only. Create on retrieval, read thereafter. No update, no delete.

### `Evidence` — *general*
> A single assertion extracted from a document, bearing on the investigation's question.

Holds the assertion text, the document it came from, an Admiralty **credibility rating** (1–6) for
the information itself, and the extraction rationale. Note the deliberate split: *reliability* (A–F)
grades the **source**, *credibility* (1–6) grades the **information** — conflating the two is the
most common misuse of the Admiralty system, and keeping them on different objects makes that
mistake structurally impossible here.

**States:** none — evidence is immutable once extracted. A re-appraisal produces a new consistency
judgement on the hypothesis side, it does not mutate the evidence.
**Invariants:** every `Evidence` references a `Document` present in the same investigation.
**CRUD:** append-only.

### `Hypothesis` — *general*
> A candidate answer to the investigation's central question, stated so that it could be disproved.

Holds its statement and its **consistency map**: for each piece of evidence, whether that evidence is
`CONSISTENT`, `INCONSISTENT`, or `NOT_APPLICABLE` with this hypothesis. That map is the ACH matrix,
held row-wise on the hypothesis rather than as a separate matrix object — the cut pass removed a
standalone `AchMatrix`, which had no behaviour a hypothesis and an investigation did not already own.

Behaviour: a weighted **inconsistency score**, where each inconsistent piece of evidence counts
according to its source reliability and information credibility. ACH ranks by *least disconfirmed*,
not most confirmed, which is precisely why this resists confirmation bias.

**Invariants:** once Direction has run, an investigation holds **at least two** hypotheses. A
single-hypothesis investigation is confirmation bias by construction. This is enforced at the
Direction transition rather than in the constructor, because an investigation legitimately exists
for the moment between opening and being directed.

Note what is deliberately *not* a hypothesis: **`INSUFFICIENT_EVIDENCE` is a `Judgment`, carried on
the `Assessment`, not a member of the hypothesis set.** A hypothesis must be stateable so that it
could be disproved; a refusal to commit makes no claim and so cannot be disconfirmed, and putting it
in the ACH matrix would let "we don't know" accumulate consistency scores against evidence it says
nothing about. Declining to conclude is a property of the conclusion, not a competing explanation.
**CRUD:** created during Direction, added to during Reflection when new leads suggest an alternative;
the consistency map is updated during Appraisal. Never deleted — a discarded hypothesis stays with
its disconfirming evidence, because *why* something was ruled out is half the analytic product.

### `Assessment` — *general*
> The analytic conclusion at one moment: a judgment, a confidence band, and the reasoning for both.

Holds the leading hypothesis, a `Judgment` (`SUPPORTED`, `REFUTED`, `PARTIALLY_SUPPORTED`,
`INSUFFICIENT_EVIDENCE`), an ICD-203 `ConfidenceBand` with its numeric probability range, and the
rationale.

Assessments are **append-only**: an investigation accumulates a *series* of them, one per analytic
phase. This is a modelling decision made for a measurement reason — confidence progression, verdict
changes, and steps-to-stable-verdict are all required metrics, and all three are unrecoverable if
the current assessment is mutated in place. A running value that gets overwritten loses its own
audit trail.

**CRUD:** append-only. No update, no delete.

### `Lead` — *ours*
> A line of enquiry that has been identified but not yet exhausted.

The statement of what needs finding out, its state (`OPEN`, `PURSUED`, `EXHAUSTED`), and its
priority. Leads are what makes the investigation *adaptive*: Collection pursues open leads,
Reflection creates new ones from what was found, and the investigation cannot converge while a
high-priority lead is still open.

**CRUD:** created during Direction and Reflection; the state is a named transition (`pursue()`,
`exhaust()`), never a generic field patch. Never deleted.

### `Step` — *ours*
> One transition of the state machine: what the agent did, what it cost, and why it moved on.

The phase that ran, the transition it produced with the reason for it, the tool calls it made, the
tokens it consumed, and the wall-clock it took. Steps are the investigation log, and they are the
**sole source of truth for every efficiency and convergence metric**. Nothing is reported that
cannot be recomputed from them.

**CRUD:** append-only.

### `Investigation` — *general*, aggregate root
> One episode of work on one subject, from opening question to findings report.

The root. Owns the subject, the leads, the documents, the evidence, the hypotheses, the assessment
series, the step series, the budget, and the current phase. Everything above except `Source` hangs
off it, and everything is saved with it — an investigation holding evidence that references a
missing document is corrupt, which is what makes this one transaction unit.

Derived behaviour lives here because the data does: `verdict_changes()`, `assessments_to_stable_verdict()`,
`tool_call_count()`, `token_cost()`, `source_diversity()`. These are read-only facts *about* the
investigation, not the agent observing its own score — reward is computed elsewhere, offline, and is
never visible to the agent.

**States:** `InvestigationPhase` — `DIRECTION`, `COLLECTION`, `APPRAISAL`, `RECONCILIATION`,
`REFLECTION`, `DISSEMINATION`, plus the terminal `COMPLETE` and `HALTED`.
**Invariants:** at least two hypotheses once Direction has run; every evidence item traces to a held
document; the assessment series never shrinks; the budget is never exceeded.

### `Budget` — *ours*
> The ceiling on one episode: maximum steps, maximum tool calls, and maximum tokens.

Three limits that always travel together and share an invariant (each positive), so they are one
concept rather than three loose integers threaded through the engine.

---

## 2. Memory

**Short-term memory is not a class.** It is the `Investigation`'s own working state — the leads,
evidence, hypotheses, and assessments accumulated so far and carried from one phase to the next. A
separate `ShortTermMemory` type would be that same state wearing a second name, so there isn't one.

**Long-term memory is the archive of past investigations, plus the `Source` register.**
`InvestigationArchive` is a repository over completed investigations; retrieval finds prior episodes
whose subject resembles the current one, by lexical similarity over the subject's name and
qualifiers.

The retrieval is deliberately lexical rather than embedding-based. Two reasons, and the second one
matters more: a vector store is infrastructure this project would have to own and justify for a
corpus of a few dozen episodes, and — more importantly — a lexical retriever *reproduces the exact
failure mode the brief asks us to measure*. "Same name, different person" is the canonical OSINT
memory-interference case, and a retriever that never confuses them would leave the harmful-retrieval
metric with nothing to detect. The weakness is the experiment.

**`MemoryMode`** — `NONE`, `SHORT`, `LONG`:

- `NONE`: each phase sees only the subject and the immediately preceding phase's output. No
  accumulated working state. This is the true baseline, not merely "long-term memory off".
- `SHORT`: the full in-episode working state carries across phases. No cross-episode recall.
- `LONG`: `SHORT`, plus archive retrieval at Direction, plus learned source reliability.

**A recalled fact is never evidence.** Retrieved memory enters an investigation as a **prior** and as
**leads to check** — clearly labelled as recall, never citable in a report. This keeps memory from
contaminating the citation set, and it makes harmful retrieval measurable rather than invisible:
if a prior sends the agent down a wrong path, that shows up as wasted steps and a wrong verdict,
with the offending prior named in the trajectory.

---

## 3. Evaluation concepts

These live on the far side of a hard boundary: the agent never imports them, and they never appear
in a prompt.

### `BenchmarkCase` — *ours*
> A subject paired with the answer a competent analyst should reach, and why.

The subject, the expected judgment, the acceptable confidence range, the facts a good report must
contain, and the trap the case is designed to catch. **The agent is handed the case's `Subject`,
never the case.** This is the ground-truth leak guard, enforced at the type level by the runner's
signature rather than by discipline.

### `StepReward` and `EpisodeReward` — *ours*
Decomposed, never a single opaque number, because the brief asks *where* investigations break down —
and a scalar cannot answer that.

- `StepReward`: information gain (new, non-duplicate evidence weighted by grade), lead progress,
  minus the step's cost. Computed offline, from the step record.
- `EpisodeReward`: verdict correctness, calibration (Brier), evidence quality, citation validity,
  appropriate abstention, minus total cost.

Both carry a frozen **version stamp**. Changing a formula bumps the version and invalidates every
number carrying the old one; results from two versions are never mixed.

### `FailureMode` — *ours*
Exactly one tag per episode, assigned offline by first-match priority over a fixed rule order:
`TOOL_FAILURE`, `MEMORY_INTERFERENCE`, `SOURCE_SELECTION`, `WEAK_EVIDENCE`, `PREMATURE_STOP`,
`NO_CONVERGENCE`, `OVERCONFIDENT`, `UNDERCONFIDENT`, `NONE`. Fixed priority keeps the taxonomy
deterministic; a case that could carry two tags always gets the same one.

### `BenchmarkRun` — *ours*
One sweep of the benchmark in one memory mode. Holds the per-case investigations and rewards, and
computes the aggregates. Failed episodes stay in the denominator with a failure tag.

---

## 4. Plumbing

Named for the external capability they adapt, not as domain concepts — this is L25's plumbing
allowance, taken deliberately and marked as such.

- `Tool` (abstract) with `WebSearch`, `Encyclopedia`, `PageFetch`. Adapters to external sources.
  `WebSearch` is keyless (DuckDuckGo's HTML front end), since no provider offers server-side search
  for free; being a `Tool` rather than a model capability is also why a search is cassette-recorded
  and replayable offline like every other retrieval.
- `Cassette`: record/replay of every external interaction, keyed by a hash of the request. **This is
  what makes the memory ablation valid** — all three modes replay byte-identical retrieval, so a
  measured delta is attributable to memory and not to the live web changing between runs. It is also
  what lets the benchmark run offline, with no API key.
- `ModelClient`: exactly one method, `decide()`. Three implementations: `LiveModel` (OpenRouter,
  provider-agnostic by construction), `RehearsedModel` (deterministic, no reasoning, the CLI's
  offline default — see `rehearsal.py`), and `ScriptedModel` (fixed replies, used only in tests).
- `Node` (abstract) and `Transition`: the state-machine contract.
- `InvestigationGraph`: the engine. Holds the phase-to-node map, runs the loop, records one `Step`
  per transition, and enforces the budget and halt conditions.

**On not using an orchestration framework:** the engine is roughly a hundred lines — a node map, a
loop, a step recorder, and halt checks. Every required metric is a hook on that loop. Taking a
framework dependency here would mean instrumenting somebody else's abstraction to recover numbers
the hand-written version exposes for free, and would hide the part of this project a reviewer most
wants to see. The laziness ladder says reuse before writing; it also says don't take a dependency
for what a few lines cover.

---

## 5. Command surface

This is a CLI, so the "API resources" question resolves to commands:

```
osint-harness cases                                  # list the benchmark subjects and claims
osint-harness investigate <case-id> [--memory MODE]  # one case; replays offline by default
osint-harness bench [--memory MODE]                  # every case in one memory mode
osint-harness ablate                                 # every case in all three modes, and compare

# Global flags, accepted before or after the subcommand:
#   --live       use the hosted model instead of the rehearsed analyst (needs OPENROUTER_API_KEY)
#   --record     allow live retrieval and write it to the cassette (otherwise replay only)
#   --max-steps  per-episode step ceiling
#   --model      OpenRouter model id (default a free-tier model; free lineups rotate, so this
#                is a flag rather than a literal baked into the client)

# Note: offline replay is the DEFAULT rather than an --offline flag, which is a stronger guarantee
# than an opt-in. There is deliberately no command to investigate an ad-hoc subject yet: every run
# is a benchmark case, so every run is scoreable. `Investigator.investigate()` already accepts any
# subject, so adding one is a CLI change only.
```

---

## 6. The cut pass

Types considered and deliberately **not** created:

| Rejected | Why |
| --- | --- |
| `ShortTermMemory` | It is the investigation's working state under a second name. |
| `AchMatrix` | No behaviour that `Hypothesis` and `Investigation` did not already own. |
| `Trajectory` | `Investigation` already owns the steps; convergence questions are methods on it. |
| `SourceReliabilityChecker`, `EvidenceEvaluator`, `ConflictResolver`, `ReportGenerator` | Verbs in noun costumes. Each is a method on the concept it acts on. |
| `Verdict` | A padding word for what is really an `Assessment`: judgment plus confidence plus reasoning. |
| `Confidence` as a bare float | It is an ICD-203 band with a range; a float loses the thing being measured. |
| A vector store for memory | Infrastructure unjustified at this corpus size, and its precision would erase the failure mode being measured. |
