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
person, and a claim all boil down to "a proposition, a set of competing hypotheses about it, resolved
by ACH." The subject kind picks the opening questions and says what each verdict means for that kind
of subject. Nothing else. There's no conditional chain branching on type anywhere in the agent.

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

### One free model, two providers, and what running it live actually taught me

`ModelClient` needs exactly one thing from a provider: turn a prompt into valid structured JSON.
Nothing else. So the harness talks to an OpenAI-compatible chat API rather than one vendor's SDK,
which made the provider a one-line swap, and OpenRouter opened up models priced at zero. That came
with a real catch: OpenRouter's free tier is rate-limited, not metered (20 requests a minute, 50 a
day with no credits ever purchased, 1,000 a day past a one-time $10 minimum), governed per account
rather than per key. The daily ceiling stopped being theoretical partway through verifying the hosted
demo, when a day of real testing used all 50 and the error said so in as many words:
`Rate limit exceeded: free-models-per-day`. NVIDIA serves the same model directly through its own
API, so `LiveModel` now speaks to both and picks whichever key is present, NVIDIA first. I measured
the difference rather than assuming it: OpenRouter needs 3.5 seconds between calls to stay under its
per-minute cap and then stops for the day, while NVIDIA took eight calls back to back with no pacing
at all, so calls through it are spaced half a second apart. Nothing about the model changed, only
which door the request goes through.

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
became `WebSearch`, a `Tool` sitting beside `Encyclopedia` and `PageFetch`. This turned out to be a
genuine improvement on the design it replaced, not a workaround forced by the provider swap. Because
search is a `Tool` now, it's cassette-recorded and replayable offline exactly like every other
retrieval, which the old model-driven search never was. That version was either a live provider call
or a scripted stand-in, nothing in between. `ModelClient` is down to exactly one method now, which is
a more honest statement of what a reasoning provider actually owes this harness.

The first version scraped DuckDuckGo's keyless HTML front end, and I marked it fragile with a
`ponytail:` comment. It was worse than fragile. DuckDuckGo answers a client that identifies itself
with HTTP 202 and a bot-challenge page, and 202 is a success code, so nothing raised, the parser found
no results in a page that had none, and every search came back empty without once being recorded as a
failure. For a while every live investigation reached exactly one publisher, Wikipedia, and reported a
clean run. I proved it with an A/B test (the harness's own user agent got 202 and nothing, a
browser's got 200 and ten results), declined to fix it by pretending to be a browser, measured
GDELT's keyless API as a replacement and rejected it (throttled on about half of all calls even ten
seconds apart), and moved to Tavily's search API with a key. `WebSearch` now treats any reply that
isn't actually results as a failed lookup, so a blocked search can't pass for an empty one again.

## 3. Key findings

**The instrumentation detects exactly what it was built to detect.** Across 42 episodes (14 cases
times 3 memory modes), all replayed from an identical cassette, `long` mode shows a 14% harmful-
retrieval rate and two memory-interference failures. `none` and `short` show zero. Since retrieval was
byte-identical across every arm, that difference is attributable to memory and nothing else. Which is
the entire point of building the cassette layer first, before anything else.

The report that run writes (`runs/results/ablation.md`) now reports the rest of what it measures
rather than computing it and keeping it to itself: per arm, mean steps, mean tool calls and failed
tool calls beside the token cost, assessments to a stable verdict and verdict changes, a reward
progression by step, a confidence progression by assessment, and every failure type listed including
the ones at zero, since a tag that never fires is information too. The token column there carries a
caveat printed next to it: offline figures are synthetic estimates from the rehearsed analyst, a
fixed count per call, not usage any provider measured. Only the live sweeps below report real ones.

**The same comparison, run once against genuine live reasoning instead of the rehearsed stand-in,
shows no measurable harm from long-term memory and a mild calibration edge, at a sample too small to
generalise from.** 6 cases (two recall pairs, two recall-free controls) across all three modes:
`long` matches `short`'s 83% accuracy but posts the best Brier score of the three (0.091 against
0.128 and 0.152), and its one irrelevant recall produced an `overconfident` tag on an otherwise
correct verdict, not a wrong answer or a memory-interference tag. Full tables, per-case verdicts, and
the caveat that a third of episodes halted on provider capacity contention while three live sweeps
shared the same allowance are in [`docs/live-results/memory-ablation.md`](live-results/memory-ablation.md).
The offline sweep above stays the one with anything demonstrated at real sample size; this one
demonstrates the same instrumentation works end to end on live reasoning, not that memory helps.

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

**A verdict is only as good as the definition behind it, and a benchmark can run for a long time
before that gap gets noticed.** The live model's actual reasoning over real evidence was correct on
Theranos and Wirecard well before it was ever scored correctly: it ruled out "clean company" and led
with "operates, but has adverse findings," which is exactly what those cases are built to test for.
The benchmark still scored both wrong, because nothing had ever told the model what `supported` or
`refuted` meant for the subject in front of it, so its own correct hypothesis and the harness's
verdict word were talking past each other. Once every subject carried a stated proposition and a
verdict standard (decision #12), and a run of live sweeps closed the specific hypothesis-wording and
reasoning gaps that surfaced (decisions #12 and #13), the same live model reached 14 of 14 on the
tuning set and 12 of 13 on a 13-case holdout set that never informed a single one of those fixes. The
one holdout miss was itself instructive: an extraction defect on pages that restate a myth before
debunking it, found only because the holdout set was never used to tune anything, which is the entire
argument for keeping one. Decision #14 fixes that defect generally rather than for the one case it
was found on: `ten-percent-brain-claim` is genuinely `refuted` now, not merely a softer wrong answer.
What it costs is a different case in the tuning set, `einstein-failed-maths-claim`, plus one new,
unexplained miss the fix doesn't obviously touch, `goldfish-memory-claim`, both landing as honest
abstentions rather than confident wrong answers. Net: 13 of 14 tuning, 12 of 13 holdout, same raw
count as before this decision, different cases. Full account, including the two remaining misses and
why I chose not to chase either further, in decision #14 in the README.

**Cost and quality don't track each other the way I expected.** Figures below are decision #14's
sweep, not the one two paragraphs up; both sweeps were rerun after that fix, so a case-by-case
comparison across the two sweeps' numbers isn't meaningful. The tuning sweep spent 352,560 tokens and
3,848 seconds of wall clock across 14 cases; the holdout sweep spent 298,841 tokens and 4,349 seconds
across 13. Per correct verdict that's roughly 27,100 tokens on the tuning set and roughly 24,900 on
the holdout set, close enough that spend doesn't obviously separate the two sweeps. The single most
expensive episode in either sweep is `openai-company` at 74,805 tokens, an unusually long Appraisal
pass that extracted 27 assertions, and it's correct; the wrong verdict on the tuning side,
`einstein-failed-maths-claim`, spent 29,163, close to the sweep's own mean. `ten-percent-brain-claim`,
now correct, is the single most expensive holdout episode at 46,496; the wrong verdict there,
`goldfish-memory-claim`, is comparatively cheap at 36,185, spent entirely on retrieval attempts that
found nothing rather than on reasoning over evidence. At the other end, the cheapest episode anywhere,
`german-emperor-claim` at 19,990 tokens, is correct. So cost still reads as a signal for difficulty,
not for correctness in either direction: the priciest episode is right, the wrong ones sit at both
ends of the spend range, and a high spend on `goldfish-memory-claim` specifically marks failed
retrieval, not hard reasoning.

**Close to half of all tool calls fail in this sweep, roughly double the share earlier sweeps saw, and
that's the fix working as designed, not a regression.** 105 of 215 in the tuning sweep, 81 of 167 in
the holdout sweep. Decision #14's Collection fix means a refusal now costs a retry against another
candidate instead of ending the round, so a case with a bad run of refusals racks up more failed calls
on the way to the same four successful reads, where the old code would have just stopped and recorded
fewer of both. Both sweeps reached the accuracy above anyway, with that failure rate priced in, which
is the part worth stating: an agent retrieving from the live web should be budgeted assuming close to
half its lookups come back with nothing once it's actually persistent about retrying, and it has to
record each one as a failed lookup rather than as an empty result, or the 202 problem from
`WebSearch` reappears in a different costume. I'm calling this an observation and not a finding
because I haven't broken those failures down by domain across a whole sweep. The runs I did read
through were 403s and 401s, sites refusing a bot or gating a paywall, not defects in the harness.

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

**Every limitation in this section up to here was written before a later session pushed the live path
much further, and I'm leaving all of it exactly as it reads rather than editing history.** Search was
fixed (decision #11), the model now runs against NVIDIA with real headroom (decision #10), and a long
run of live sweeps closed most of the reasoning gaps a wider evidence base actually exposed (decisions
#12 and #13), then a still later pass fixed a general extraction-polarity defect at the cost of one
tuning case (decision #14). The result: 13 of 14 on the tuning set, 12 of 13 on a held-out set that
never informed any of it, both remaining misses an honest abstention rather than a confident wrong
answer. What follows is what's genuinely still true as of that later work, not superseded by it.

**Reward weights are argued, not derived.** 0.40 / 0.25 / 0.20 / 0.15 is a defensible position about
what matters most, but I haven't run a sensitivity analysis on it. A different reviewer could easily
argue for different weights, and right now the harness doesn't show how conclusions shift under them.

**Resolved since this was written, decision #14 in the README has the full account.** This entry
originally described a specific extraction defect the holdout set caught, Appraisal reading a myth
as asserted from pages that were actually debunking it, and named fixing it as next work rather than
done work, specifically to avoid tuning against the one holdout observation that found it. It's fixed
now, generally rather than for that one case, verified by rerunning both the tuning and holdout sets
in full rather than just the case that surfaced it. `ten-percent-brain-claim` is genuinely `refuted`
now; the fix costs a different tuning case and coincides with one new, unexplained holdout miss,
`goldfish-memory-claim`, both discussed in decision #14 rather than left for later.

**The benchmark is small, and English-only.** Fourteen tuning cases and thirteen held-out ones are
enough to exercise every trap more than once and to catch real generalisation gaps, as the one above
shows, but still not enough for tight statistical confidence in any single rate. Non-English sources,
jurisdictional records, beneficial-ownership chains: all untested.

**Convergence is barely exercised.** Since the stand-in analyst reaches the same verdict every time,
verdict-change and assessments-to-stable-verdict both sit at zero in the shipped run. The metrics
themselves are implemented and tested, the shipped data just can't demonstrate them. They're printed
regardless now, beside a reward progression and a confidence progression that are flat for the same
reason; the versions with something in them are the live sweeps in section 3. One small thing did
get caught here: an independent audit found this metric was indexing the assessment series while
labelled and reported as a step count, so a six-step episode was reading as "settling at step 0",
which was just wrong.

## 5. What I'd do next, in order

1. **Done: Appraisal's extraction-polarity gap is fixed (decision #14)**, verified against both myth
   cases and the full tuning and holdout sets; `ten-percent-brain-claim` is genuinely `refuted` now.
   Two open items remain, named in that same decision rather than hidden. `einstein-failed-maths-claim`
   is a source-selection shortfall upstream of Appraisal: a wide, mostly-tangential search surfaces
   plenty of adjacent pages for a claim like this one and only ever one genuinely on-topic page.
   Improving the search-query and reading-choice prompts for topical precision, then resweeping both
   sets the same way #14 was checked, is the next concrete step there, not a further loosening of how
   much evidence counts as sufficient. `goldfish-memory-claim` is a different shape entirely, total
   retrieval failure rather than a thin pool, confirmed on two clean reruns with no mechanism found yet;
   it needs its own investigation, not the same fix as the first case.
2. **Extend the live ablation from 6 cases to the full 42-episode set.** A first live run
   ([`docs/live-results/memory-ablation.md`](live-results/memory-ablation.md)) already confirms
   the instrumentation works end to end on genuine reasoning and shows no measurable harm from
   memory, but at n=6 per arm, with over a third of episodes cut short by shared provider load, it
   isn't a sample size anything can be generalised from. The full sweep, run without three other
   live processes competing for the same allowance, is what would actually settle it.
3. **Add a second and third real source type** beyond what Tavily's search surfaces on its own: a
   company registry, and a news archive, so evidence quality stops depending entirely on what one
   search API happens to rank first.
4. **Run the reward weights as an actual sensitivity sweep** instead of just asserting them, and report
   which conclusions hold steady across weightings and which ones are just artifacts of the choice.
5. **Repeat each case a few times** to separate model variance from genuine memory effects. This design
   makes that cheap already, since the cassette holds retrieval fixed and only sampling varies.
6. **Grow the archive on purpose** to test memory at a scale where interference gets more likely, and
   check whether the harmful-retrieval rate actually rises with archive size the way it should.
7. **Add an adversarial case class**: a subject whose public record is deliberately contradictory
   across sources of different reliability, to push the conflict path harder than the current cases do.
