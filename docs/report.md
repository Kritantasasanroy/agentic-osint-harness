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

### Refusal fallbacks left off, against the vendor's own advice

The provider recommends enabling server-side fallbacks, which re-run a declined request on another
model. For a product that is sensible; for a benchmark it is corrupting, because it silently places
episodes produced by two different models into one comparison. Refusals raise, get tagged, and stay
in the denominator.

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

**The live path is unverified.** No API key existed in the build environment. `LiveModel.decide()`
and `LiveModel.search()` have never executed; their response parsing is tested, the call-and-charge
glue is not. This is the single largest gap and it is not closable offline.

**Every shipped number comes from a non-reasoning stand-in.** The rehearsed analyst retrieves real
documents and produces real citations but does no analysis, so it abstains everywhere and scores 21%
— correct only on the three abstention cases. These numbers validate the harness, not the agent, and
should not be read as agent quality.

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

1. **Run the live recording pass.** One command and a key. It closes the verification gap, replaces
   every number in this report with a meaningful one, and populates the cassette with real web
   search alongside the encyclopedia.
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
