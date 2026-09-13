# Report: design choices, findings, limitations

## 1. The choice that shaped everything else

The brief asks for a findings report "a human analyst would actually trust enough to act on." An
analyst doesn't trust a number some language model happened to emit. They trust a judgment they can
audit themselves. So my first real decision was to stop inventing scoring from scratch and borrow the
domain from a field that already solved this problem: intelligence analysis.

Three instruments, each one external to this codebase and checkable against a published standard:

- **Admiralty (NATO) grading** splits reliability of the *publisher* (A through F) from credibility of
  the *claim* (1 through 6). They're graded on different criteria, and conflating them is the standard
  way people misuse this scale, so I kept them on different objects. They can't get merged by accident.
- **Analysis of Competing Hypotheses** ranks explanations by least *disconfirmed*. This flips the usual
  failure mode on its head: evidence consistent with five hypotheses is weak evidence, evidence that
  eliminates one is strong. Confirmation bias gets designed out here, not just warned against.
- **ICD 203 estimative probability** turns confidence into a named band with a numeric range attached,
  which is the only reason it can be Brier-scored at all.

One thing this bought me for free: all three subject kinds collapse into one mechanism. A company, a
person, and a claim all boil down to "a set of competing hypotheses, resolved by ACH." The subject
kind only picks the opening questions. Nothing else. There's no conditional chain branching on type
anywhere in the agent.

## 2. Design choices and why I made them

### Owning the state machine myself

About a hundred lines, hand-written, nothing fancy. The brief asked for an explicit graph, and every
metric it wants turns out to be a hook on that loop: one `Step` per transition, carrying the phase, the
reason it moved, the tool calls, the tokens, the latency. If I'd reached for a framework instead, I'd
have spent my time instrumenting somebody else's abstraction just to recover numbers this version
gives me for free.

### Cassettes as an integrity mechanism, not a speed trick

The obvious read on record and replay is "it makes tests fast." That's not why it's here. The memory
ablation compares three arms, and unless all three see identical retrieval, any difference between
them is partly just the web changing between runs. So replay is the default. A cassette miss raises an
error instead of quietly falling through to the network, because a silent live call would wreck the
one guarantee this whole layer exists to give.

Side effect worth having: the whole benchmark runs with no API key, which is why anyone reviewing this
can run it before reading a word of this document.

### Deliberately imperfect memory retrieval

Recall here is lexical, not embedding-based. Looks like a shortcut. Isn't one. A retriever smart enough
to never confuse two people sharing a name would make the harmful-retrieval metric impossible to
measure, and same-name confusion happens to be the canonical way memory fails in real OSINT work. The
weakness is the experiment. The measured ablation backs this up: interference shows up only in `long`
mode, on similarly named subjects, and gets attributed automatically because a `Recollection` always
carries the episode it came from.

### Two guarantees I made structural instead of procedural

Both of these could have just been prompt instructions. Prompt instructions aren't guarantees, though,
they're requests.

1. **Memory can never get cited.** The archive stores conclusions and holds no document, no URL,
   nothing citable, but that alone turned out not to be enough. An independent auditor assembled an
   `Investigation` outside the guarded path and rendered a clean-looking forged citation with it. So
   now the guarantee rests on `Evidence` having exactly one construction site behind
   `record_evidence`, plus a validator that refuses to build or reload any record holding an unbacked
   citation.
2. **Ground truth can never reach a prompt.** The investigator's signature accepts a `Subject`.
   Nothing accepts a `BenchmarkCase`. The object holding the actual answer never crosses that boundary
   at all.

### Scoring that can't be gamed or quietly shifted

Reward is decomposed: correctness at 0.40, calibration at 0.25, evidence quality at 0.20, citation
integrity at 0.15, minus a capped efficiency penalty. Computed after the episode, from the stored
trajectory, version-stamped. Nothing the agent can reach ever imports the scoring code.

Two decisions worth calling out inside that:

- **Calibration is half Brier score, half defensible-band membership.** Brier alone gets minimised by
  hedging every answer to even odds, which is exactly the behaviour an investigator should never get
  rewarded for. The band term punishes hedging and overclaiming both.
- **Abstention scores zero on correctness in either direction.** Declining when a verdict was actually
  available, and committing when abstention was the only honest answer, both score zero on
  correctness. No partial credit, while a supported-versus-partially-supported miss still earns half.
  The other components still contribute, and that's deliberate: a wrong verdict resting on real,
  well-graded sources is not the same failure as one built on invented sources.

### No fallback routing across models

OpenRouter can quietly re-route a failed or declined request to a different model behind the same
call. I switched that off. For a product, that tradeoff is sensible. For a benchmark, it's corrupting,
because it silently mixes episodes from two different models into one comparison. A failure just
raises, gets tagged, and stays in the denominator instead of vanishing.

### OpenRouter, one specific free model, and what running it live actually taught me

`ModelClient` needs exactly one thing from a provider: turn a prompt into valid structured JSON.
Nothing else. So the harness runs on OpenRouter rather than a single vendor's API, which makes the
provider a one-line swap and opens up models priced at zero. That comes with a real catch: the free
tier is rate-limited, not metered (20 requests a minute, 50 a day with no credits ever purchased,
1,000 a day past a one-time $10 minimum), governed per account rather than per key. `LiveModel` paces
calls to respect the per-minute ceiling. The daily ceiling can't be lifted from inside the harness at
all, which is exactly why the live results below are a deliberately bounded run, not the full sweep.

I picked the model (overridable through `--model`) by checking OpenRouter's own catalogue directly.
Good call, too: the research for this turned up a wall of confident-sounding blog rankings naming
models that don't even appear in that catalogue at all. A small lesson of its own about trusting
secondary sources for anything that changes fast. OpenRouter says plainly that its free lineup
rotates without notice, which is exactly why the model id is a parameter, never a literal.

Running it live surfaced something no offline test ever could, and then surfaced it twice. My first
pick, `nex-agi/nex-n2.5-pro:free`, spends the large majority of its token budget on hidden
chain-of-thought before writing anything (I measured 319 of 397 completion tokens burned on a plain
prompt). Capping reasoning effort and raising the output ceiling made short prompts work, so I
considered it handled.

It wasn't handled. On the heavier prompts deeper into an investigation, the ones carrying a full
briefing of accumulated leads and hypotheses, the same model still burned its entire budget thinking
and returned an empty completion, after sitting on the request for close to six minutes. Direction
succeeded; Collection died every time. The capability flags in the catalogue said nothing about this,
because the flags describe what a model accepts, not how it behaves under load.

So I measured instead of guessing: I ran the real Collection prompt and schema through candidates
directly. `nvidia/nemotron-3-super-120b-a12b:free` returned a correct, well-formed plan in **3.9
seconds** against the same prompt that had cost the previous model 350 seconds and produced nothing.
That is the default now. The generalisable finding is that "free model supporting structured output"
is not a specification: two models with identical capability flags in one catalogue differed by two
orders of magnitude on the only workload that mattered, and nothing short of running the real prompts
through both would have revealed it.

### Search became a real tool, not a model capability

No provider offers server-side search for free. So search moved out of `ModelClient` entirely and
became `WebSearch`, a `Tool` sitting beside `Encyclopedia` and `PageFetch`, using DuckDuckGo's keyless
HTML front end. This turned out to be a genuine improvement on the design it replaced, not a
workaround forced by the provider swap. Because search is a `Tool` now, it's cassette-recorded and
replayable offline exactly like every other retrieval, which the old model-driven search never was.
That version was either a live provider call or a scripted stand-in, nothing in between. `ModelClient`
is down to exactly one method now, which is a more honest statement of what a reasoning provider
actually owes this harness. The scraping approach is fragile to markup changes by construction, and I
marked it that way with this project's own `ponytail:` convention, naming the ceiling and the upgrade
path right there in the comment.

## 3. Key findings

**The instrumentation detects exactly what it was built to detect.** Across 42 episodes (14 cases
times 3 memory modes), all replayed from an identical cassette, `long` mode shows a 14% harmful-
retrieval rate and two memory-interference failures. `none` and `short` show zero. Since retrieval was
byte-identical across every arm, that difference is attributable to memory and nothing else. Which is
the entire point of building the cassette layer first, before anything else.

**Failure attribution is informative, not decorative.** The dominant tag in the shipped run is
`source_selection` (11 of 14 in both `none` and `short`), correctly flagging that every investigation
rested on a single publisher. A true statement about the run, and exactly the kind of finding a plain
scalar reward would have buried.

**Making abstention a first-class verdict changes the shape of the whole benchmark.** Three of
fourteen cases have abstention as the *correct* answer, and one, Michael Jordan the researcher, is
built specifically so abstaining is *wrong* despite a famous namesake making it tempting. Without
those floor cases, an agent that abstains on everything would look cautious instead of useless.

**Per-slice review wasn't enough, and the final audit proved it.** Eight slice-by-slice audits all
came back clean. Then one final independent audit over the whole codebase came back with violations,
and the worst one was invisible to every round before it: `Reconciliation` and `Dissemination` were
reading evidence through accessors that bypassed the memory gate, so the `none` control arm was
leaking exactly the state it's supposed to be defined by withholding. It survived because the test
asserted on the briefing header instead of the prompt actually sent to the model.

That same audit found long-term memory persisting between separate invocations, so the harmful-
retrieval rate moved from 14% to 29% on a second run of the identical command. The headline number was
really just an artifact of how many times the thing had been run. It also found a metric indexing the
assessment series while labelled as a step count, the Admiralty grading's *reason* field generated and
then thrown away, and the narrative half of the report carrying no grounding check at all.

Every one of those is fixed now, each with a regression test naming the exact defect. `ablate`
produces byte-identical output across repeated invocations, and I verified that directly.

**The gate then failed two more times, on the fixes themselves.** Round two caught me having removed
the render-time citation check. I'd accepted a static claim that it duplicated the constructor
validator, without ever testing that claim myself. An auditor broke it in three lines: pydantic simply
doesn't revalidate on mutation into a held dict, so a forged citation still rendered clean. Round three
caught a defect I'd introduced with my own fix from round two: gating hypotheses by memory mode meant
a Reflection-added hypothesis could never actually be judged, so it carried a disconfirming score of
zero, and under least-disconfirmed-wins, that beat every hypothesis that was genuinely examined. An
unexamined guess would have been reported as the leading explanation, and nobody would have known why.

Four lessons here generalise well beyond this one project:

1. **Slice-local review can't see a property that only breaks from the interaction between slices.**
   The `none` leak needed two separate phases and a shared helper to exist at once. No single slice
   contained it on its own.
2. **A test that asserts on an intermediate value passes while the real property is already false.**
   The memory test checked `Briefing.header()`, which was correctly gated, so it never once noticed
   that the phases themselves were bypassing it. It now asserts on the actual prompt sent.
3. **A demonstrated break outranks a static claim of redundancy.** Two auditors disagreed about the
   renderer check. The one holding a working exploit was right, and I'd sided with the other one.
4. **Fixes need auditing too.** The single worst defect found anywhere in this project, an untested
   hypothesis winning by default, was introduced by a fix. Not by the original build.

## 4. Limitations

**The shipped benchmark numbers still come from the non-reasoning stand-in, on purpose.** The free
tier's daily cap (50 calls a day, no credits purchased) can't support a 42-episode live sweep in one
sitting, so the reproducible submission artifact stays the rehearsed analyst: real documents, real
citations, zero actual analysis. It abstains everywhere and scores 21%, correct only on the three
abstention cases. These numbers validate the harness. They say nothing about agent quality, and
shouldn't be read that way.

**The live path itself, though, is verified now. Not simulated.** A real `--live --record` run
completed Direction, Collection, Appraisal, Reconciliation, and Reflection in full, against a real
free model. Reflection judged the evidence incomplete and looped back into a second round of
Collection, producing 18 genuinely graded assertions, 72 real ACH judgments, and a substantive
`supported` verdict with specific, evidence-citing reasoning along the way, before that sixth call
exceeded the token ceiling and the crash-tolerant halt caught it cleanly. I kept the transcript at
`docs/example-live-run/`. What's still unverified is a complete, uninterrupted multi-phase episode
that actually reaches Dissemination, and the full 42-episode sweep. Both blocked by the same daily
quota, not by anything the design leaves untested.

**Running it live found a real bug no mock ever could have.** Appraisal's source gradings were genuine
and well-reasoned, but kept vanishing from the report, because the model wrote the domain field as a
hostname plus a description, and evidence gets keyed by the bare hostname alone. Fixed at the point of
use, with a regression test behind it, see decision #10 above for the full story. This is the clearest
evidence in the whole project that live verification catches a different class of defect than review
does: `ScriptedModel` and `RehearsedModel` simply cannot write free text into a structured field, so
neither one could ever have produced this failure, no matter how carefully either was reviewed.

**Reward weights are argued, not derived.** 0.40 / 0.25 / 0.20 / 0.15 is a defensible position about
what matters most, but I haven't run a sensitivity analysis on it. A different reviewer could easily
argue for different weights, and right now the harness doesn't show how conclusions shift under them.

**Only one real source in the offline cassette.** The recorded retrievals are Wikipedia only, since it
needs no key. Source-diversity scores in the shipped run are structurally capped at one because of
this, which is exactly why `source_selection` dominates the failure counts. A live recording pass with
real web search fixes this.

**The benchmark is small, and English-only.** Fourteen cases is enough to exercise every trap once,
but not enough for real statistical confidence in any single rate. Non-English sources, jurisdictional
records, beneficial-ownership chains: all untested.

**Convergence is barely exercised.** Since the stand-in analyst reaches the same verdict every time,
verdict-change and assessments-to-stable-verdict both sit at zero in the shipped run. The metrics
themselves are implemented and tested, the shipped data just can't demonstrate them. One small thing
did get caught here: an independent audit found this metric was indexing the assessment series while
labelled and reported as a step count, so a six-step episode was reading as "settling at step 0",
which was just wrong.

## 5. What I'd do next, in order

1. **Clear the daily quota over a few days, or fund the account past the $10 mark**, and run the full
   42-episode live ablation. The live path already proved it works end to end. What's missing is
   volume, not verification. At 1,000 requests a day past $10 in lifetime credits, a full sweep turns
   into a same-day thing instead of a multi-day one.
2. **Add a second and third real source type** so source diversity can climb past one and
   `source_selection` stops dominating everything: a company registry, and a news archive.
3. **Run the reward weights as an actual sensitivity sweep** instead of just asserting them, and report
   which conclusions hold steady across weightings and which ones are just artifacts of the choice.
4. **Repeat each case a few times** to separate model variance from genuine memory effects. This design
   makes that cheap already, since the cassette holds retrieval fixed and only sampling varies.
5. **Grow the archive on purpose** to test memory at a scale where interference gets more likely, and
   check whether the harmful-retrieval rate actually rises with archive size the way it should.
6. **Add an adversarial case class**: a subject whose public record is deliberately contradictory
   across sources of different reliability, to push the conflict path harder than the current cases do.
