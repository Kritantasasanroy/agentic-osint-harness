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
pytest && mypy && ruff check .                       # 288 tests, strict types, clean lint
```

Reports land in `runs/<case>-<memory>/findings.md`, and the ablation report in
`runs/results/ablation.md`.

**One thing to be upfront about: what you're running offline is the machinery, not the analysis.**
Without an API key, the harness falls back to a rehearsed analyst. It does real retrieval, produces
real citations, but it doesn't reason, so it abstains on every single case. I did that on purpose,
and I explain why down in [Honest status](#honest-status-whats-actually-verified-and-what-isnt).
Want the real thing instead?

```bash
export NVIDIA_API_KEY=...       # preferred: same model, direct, far higher throughput
# or: export OPENROUTER_API_KEY=...
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

There's a second way in, on the **Check a document** tab. Upload a PDF, a Word file, HTML, Markdown
or plain text (up to 5 MB) and the agent reads it, picks out the one main factual claim it makes,
restates that claim so it stands on its own, and then investigates it exactly the way it
investigates a benchmark claim: against sources it finds on the live web. The document never gets
to count as evidence for itself. It only decides what gets checked, and the investigation never
sees it. If there's nothing checkable in it, an opinion column say, it tells you that instead of
inventing a claim to check. Every document you check lands under **My documents** with its claim,
verdict, confidence and full dossier. That list lives in your own browser rather than on the server,
which keeps one visitor's uploads away from the next and survives the free backend going to sleep.
There's no expected answer to score an upload against, so you get a verdict and a report there,
never a right-or-wrong mark.

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

### 10. Two providers, one model, chosen by measurement rather than preference

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

Then that daily ceiling stopped being theoretical. Partway through verifying the hosted demo, a run
came back with `Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model
requests per day`. Fifty requests is about four full investigations, and a day of genuine testing had
spent them. So `LiveModel` now speaks to **either** provider: NVIDIA serves the identical model
directly, and it is preferred whenever `NVIDIA_API_KEY` is set, with OpenRouter kept as the fallback
so nothing that already worked stops working. The difference is not subtle. OpenRouter's free tier
forced a 3.5-second gap between calls to respect 20 a minute and then cut me off for the day; NVIDIA
took eight back-to-back requests with no pacing at all in the same session, so its configured gap is
0.5 seconds. Same model, same prompts, same JSON, roughly an order of magnitude more headroom.

That is the provider neutrality this design claimed from the start finally being cashed in rather
than asserted: switching cost one constructor argument and an environment variable, because
`ModelClient` only ever wanted structured JSON and never cared who produced it. The one thing I did
have to fix was honesty in the error path, since every failure message said "OpenRouter reported an
error" regardless of who actually answered. It names the real provider now.

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

I originally backed that `Tool` with DuckDuckGo's keyless HTML front end and left a `ponytail:` note
saying to swap in a real search API if reliability ever became the bottleneck. It became the
bottleneck, in the most instructive way possible. Running the hosted demo showed every live
investigation reaching exactly one source, Wikipedia, and reporting it as a clean run. The cause was
not flaky markup: DuckDuckGo answers a self-identifying client with **HTTP 202 and a bot-challenge
page** instead of results. 202 is a success code, so `raise_for_status` stayed silent, the parser
found no results in a page that genuinely had none, and a totally blocked search reported itself as
a search that simply found nothing. A browser User-Agent got 10 results from the same endpoint
immediately, which told me exactly what was happening and also told me not to do that: spoofing a
browser to get around a bot challenge is both fragile and not something I want in a submission.

I measured GDELT's keyless API as the honest replacement and rejected it on evidence, not vibes: 429
on roughly half of all calls even paced ten seconds apart, and 50 to 80 seconds per search once
retries were counted. So `WebSearch` now uses Tavily with a real key. Live runs since go from one
source to seven, across `bbc.com`, `bl.uk`, `nobelprize.org`, `justice.gov`, `pmc.ncbi.nlm.nih.gov`
and others. The generalisable lesson is the one about 202: a success code with an empty body is a
worse failure than an error, because nothing anywhere reports it. `WebSearch` now treats a reply
that is not parseable results as an explicit failure, so a blocked search can never again look like
an honest empty one.

### 12. A verdict needs to say what it's a verdict on, or the model will guess

Once search actually worked, I could finally see the model reason over real, diverse evidence
instead of one Wikipedia page, and that's what surfaced the next problem: the harness never told the
model what `supported` or `refuted` actually *meant* for the subject in front of it. Every subject
opened with a `SUBJECT (kind): descriptor` line and nothing else. On Theranos and Wirecard, both
adverse-media cases, the model weighed the evidence correctly, ruled out "clean company" and led
with "operates, but has adverse findings", then wrote `supported`, meaning that hypothesis was
supported. The benchmark reads the same word as a verdict on the company being clean. Correct
analysis, scored wrong, because nothing had ever said which reading applied.

So every subject now carries a `proposition_under_test()`, the one sentence a verdict is actually a
verdict on, and a `verdict_standard()` saying in plain terms what each of the four judgments asserts
about that kind of subject. Both go into the prompt every phase sees, in every memory mode, for the
same reason the opening hypotheses were never gated: they state the task, they don't accumulate
findings, so hiding them wouldn't model a weaker memory, it would just remove the question.

Writing that standard down took several tries before it actually worked, and each miss taught me
something specific rather than something vague:

- **Direction was quietly replacing the subject's own hypotheses.** Fewer than two offered fell back
  to the subject's set, but two or more replaced it outright, so a model that offered its own four
  could silently swap out the very hypotheses written to cover every verdict. Fixed: the subject's
  hypotheses are always kept, and the model can only add up to two genuinely new ones on top,
  de-duplicated against what's already there.
- **A confident verdict with no reasoning behind it could still overwrite a correct one.** Every
  field of the reconciliation reply has a default, so an empty object validates as a reply. Twice,
  a reasoned earlier verdict got silently replaced: once by an entirely empty reply defaulting to
  `insufficient_evidence`, once by a reply that rescored a real thirty-pair ACH matrix and reversed a
  well-reasoned `partially_supported` to `refuted` with an empty rationale explaining neither the
  reversal nor the verdict. An assessment is a judgment, a confidence, and the reasoning for both,
  the same standard already enforced for a source's reliability grade; a reply with no rationale now
  changes nothing, not the verdict and not the matrix underneath it.
- **A compound claim needs its core event named, not just implied.** "Einstein was awarded the Nobel
  Prize for his theory of relativity" is true about the prize and false about the reason. Three
  rounds of live testing called it `refuted` anyway, because "the assertion is inaccurate as stated"
  and "the assertion is partly accurate but misleading as stated" are both trivially true the moment
  any part of a compound claim is wrong, so nothing separated a hypothesis about the reason being
  wrong from a hypothesis about the whole thing being wrong. What worked was naming the mechanical
  test directly in the standard: set aside the clause giving the reason, date, place, manner or
  actor, and judge what's left. Three of three retests after that came back `partially_supported`,
  where four straight attempts before it hadn't.
- **A false premise needs to be its own hypothesis, explicitly, or "wholly false" absorbs it.** The
  King of France case had been correct twice, then flipped to `refuted` once "did not happen or does
  not hold at all" got sharper, because that phrasing reads just as naturally as "the premise itself
  is unreal." Both the wholly-false and partly-true hypotheses now explicitly presuppose a real
  premise, so a claim about something that doesn't exist routes to its own hypothesis instead of
  getting swept into "wholly false."
- **Finding a lot about someone doesn't mean you found the right someone.** With no qualifiers,
  John Smith should be unresolvable, but a run that finally completed all ten steps (instead of
  halting partway on a transient provider error, once the retry logic below existed to get it there)
  retrieved a clean, abundant, internally consistent record for the historical Captain John Smith
  and reported `supported`, reasoning that "all sources describe the same historical figure." The old
  hypothesis asked whether the record *conflates* distinct people, a property of how well the
  investigation goes; the real question is whether the query itself, name plus whatever qualifiers
  were given, distinguishes one person from the others who share the name. An abundant record for one
  candidate doesn't answer that. Reworded, and three of three retests landed correctly at
  `insufficient_evidence`.

None of this touches the offline path. `RehearsedModel` never writes a judgment or a hypothesis of
its own, so the ablation numbers are unaffected; I reran it and the report is still byte-identical.

### 13. Two more failures the same fixed-code sweeps needed catching

A couple of things surfaced purely from finally running enough long, uninterrupted investigations
back to back, unrelated to what each one is actually reasoning about:

- **A transient provider error used to end the whole investigation.** With a few running at once,
  NVIDIA's endpoint would occasionally answer "Service temporarily overloaded" or 429 within seconds
  of a call starting, and the halt landed wherever the investigation happened to be, sometimes
  minutes of real work lost to a condition that clears itself. `LiveModel` now waits out a throttle or
  a server error a few times, each pause longer than the last, before it counts as a real failure; a
  request rejected on its own merits still fails immediately, and a connection that never completes
  at all now becomes the same kind of halt a bad reply always did, rather than an unhandled
  transport error.
- **Reflection held back too little budget to actually reach a report.** It reserved two steps before
  sending an investigation back for more evidence, but a full round, collection through reflection
  again, costs four, plus one more to write the report. Under a twelve-step ceiling, a third round
  reliably ran the clock out one step short of Dissemination. Three of the four live investigations in
  the previous round of testing halted with no report at all for exactly this reason. Fixed by
  actually counting what a round and a report cost before agreeing to another one.
- **A page fetch that hit a PDF, or a page carrying an unrecognised HTML marked section, could ruin
  or crash the whole run.** One investigation's evidence included three PDFs read as if they were
  HTML, hundreds of thousands of characters each, roughly half of them raw binary, all treated as
  citable text. A later run crashed outright on a byte sequence starting `<![`, which Python's HTML
  parser doesn't recognise. `PageFetch` now refuses anything that isn't declared as a text or HTML
  page, as a failed lookup rather than bad evidence, and the parser escapes an unrecognised marked
  section instead of raising on it.

---

## The benchmark

14 cases: 5 companies, 4 people, 5 claims. I picked them to be adversarial, not just varied for
variety's sake. Every judgment type and every trap gets exercised at least once, and a test fails the
moment that stops being true. A second, 13-case set lives at
[`benchmark/holdout.json`](benchmark/holdout.json), the same shape and the same traps on different
subjects, and it never once informed a fix while I was tuning the prompts above. It exists to answer
one question honestly: did fixing the 14 cases I could see teach the model something general, or just
memorise those 14 answers.

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

**Verified, with commands you can run yourself:** 288 tests pass, `mypy --strict` comes back clean
across 43 source files, `ruff check` is clean, and a full 42-episode ablation (14 cases times 3
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

That 21% is the machinery running with no reasoning behind it at all, and it stays true for exactly
that reason: it's the floor, not the ceiling. What the live model itself actually reaches, once it's
given a real reason to reason and the fixes below are all applied, is decision #12 and #13's story,
ending in a 14-of-14 tuning sweep and a 12-of-13 sweep against 13 cases none of that tuning ever saw.
Read on for how the story gets there.

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

### What three more live runs looked like, once search actually worked

After replacing the blocked search backend (decision #11) and moving to NVIDIA (decision #10), I ran
three more full investigations end to end. These are the real numbers, not a best-of:

| Case | Verdict | Expected | Brier | Sources | Tool calls | Failed | Tag |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ada-lovelace-person` | supported | supported | 0.09 | 7 | 27 | 5 | no_convergence |
| `theranos-company` | supported | refuted | 0.61 | 3 | 27 | 10 | no_convergence |
| `einstein-nobel-relativity-claim` | refuted | partially_supported | 0.85 | 3 | 16 | 0 | overconfident |

What I want to be precise about is which half of that is the harness and which half is the model.
The harness half worked: zero crashes, zero silent failures, every `web_search` call returning real
results, source diversity going from one to seven, ACH scoring 12 to 48 evidence-hypothesis pairs
per round instead of the zero that a real bug used to produce, `record_evidence` visibly refusing
citations that did not trace to a retrieved document, and the Einstein run completing all six phases
through Dissemination with 16 of 16 tool calls succeeding. Every failed tool call above is a real
site refusing a bot: `sec.gov`, `britannica.com` and `nytimes.com` returning 403, the WSJ returning
401 at a paywall. Those are honest retrieval failures, correctly recorded rather than hidden.

The model half is where it gets interesting, and I am not going to dress it up. One of three verdicts
was right. Theranos, a case whose whole point is adverse media, came back `supported` on real
evidence from `justice.gov` and Wikipedia. Einstein got the underlying fact right (the prize was for
the photoelectric effect) but forced it into `refuted` at 0.92 confidence when the honest answer is
`partially_supported`, and the failure taxonomy caught exactly that and tagged it `overconfident`.
That is the system working as designed: a free 120B model reasoning imperfectly over real evidence is
the thing being measured, and the point of Brier scores and failure tags is that a wrong answer
arrives labelled as one rather than quietly passing for a right one.

### What actually turned the model half around

Looking closely at that one-of-three result is what led to decision #12. Theranos wasn't a reasoning
failure at all: the model correctly ruled out "clean company" and correctly led with "operates, but
has adverse findings", then wrote the word `supported`, meaning that hypothesis. The benchmark reads
`supported` as a verdict on the company being clean. Nothing had ever told the model which reading
was meant, on any of the 14 cases, the whole time.

Once every subject carried a stated proposition and a verdict standard, I ran full live sweeps
against all 14 cases repeatedly: fix a specific defect a miss pointed at, sweep again, keep whatever
stopped failing and kept passing, throw out nothing that already worked. It took several sweeps to
land on the fixes decisions #12 and #13 actually describe, and I'm not going to pretend I remember
the exact miss list of every intermediate round precisely enough to print it here. What I can stand
behind precisely is the state everything above converged to, and how I checked it wasn't a fluke.

<!-- osint-harness:tuning-summary -->
**Final sweep, one investigation at a time, exactly matching how the hosted demo runs (it also
processes one job at a time): 14 of 14, zero halts, zero crashes.**

| Case | Verdict | Confidence | Steps |
| --- | --- | --- | --- |
| anthropic-company | supported | 0.90 | 10 |
| openai-company | supported | 0.80 | 10 |
| theranos-company | refuted | 0.95 | 10 |
| wirecard-company | refuted | 0.92 | 10 |
| vantage-nebula-company | insufficient_evidence | 0.50 | 10 |
| ada-lovelace-person | supported | 0.92 | 10 |
| satya-nadella-person | supported | 0.95 | 10 |
| john-smith-person | insufficient_evidence | 0.70 | 10 |
| michael-jordan-researcher-person | supported | 0.80 | 10 |
| great-wall-from-space-claim | refuted | 0.82 | 10 |
| einstein-failed-maths-claim | refuted | 0.95 | 10 |
| einstein-nobel-relativity-claim | partially_supported | 0.95 | 10 |
| king-of-france-claim | insufficient_evidence | 0.95 | 10 |
| apollo-11-date-claim | supported | 0.99 | 6 |

The two cases that took the most iteration were `einstein-nobel-relativity-claim`, wrong on every
sweep before the "set aside the clause" fix landed, and `john-smith-person`, right on early sweeps
only because a transient provider error halted it before it ever reasoned, then wrong once it
actually ran to completion, on the same disambiguation mistake the case exists to catch. Both went
3 for 3 in a focused, repeated re-test once the fix that actually addressed each one landed, and
correct again here on top of that. That's the evidence this wasn't a lucky single sample: the same
case, rerun independently, landing the same way every time.

None of this, not one fix, one wording change, or one rerun, was ever checked against the 13
held-out cases in [`benchmark/holdout.json`](benchmark/holdout.json). The question that actually
answers "did this generalise, or did I just memorise 14 answers" is what a single sweep against that
set, run only after every change above was already frozen, comes back with:

**Holdout sweep, same conditions, cases never seen during any of the tuning above: 12 of 13.**

| Case | Verdict | Confidence | Steps |
| --- | --- | --- | --- |
| ftx-company | refuted | 0.92 | 10 |
| enron-company | refuted | 0.92 | 10 |
| raspberry-pi-company | supported | 0.92 | 10 |
| quorvane-meridian-company | insufficient_evidence | 0.50 | 10 |
| marie-curie-person | supported | 0.95 | 10 |
| jensen-huang-person | supported | 0.95 | 10 |
| david-jones-person | insufficient_evidence | 0.50 | 10 |
| michael-collins-astronaut-person | supported | 0.96 | 10 |
| goldfish-memory-claim | refuted | 0.92 | 10 |
| armstrong-1968-claim | partially_supported | 0.80 | 10 |
| eiffel-tower-1889-claim | supported | 0.95 | 10 |
| german-emperor-claim | insufficient_evidence | 0.95 | 10 |
| **ten-percent-brain-claim** | **supported** (wrong; expected `refuted`) | 0.75 | 10 |

The same-name trap (`david-jones-person`) and the disambiguation trap
(`michael-collins-astronaut-person`), the exact two shapes `john-smith-person` and
`michael-jordan-researcher-person` exist to catch, land correctly on subjects the fix was never run
against. So does the reason-versus-core-event shape (`armstrong-1968-claim`, right about the
landing, wrong about the year, correctly `partially_supported`) that `einstein-nobel-relativity-claim`
took four attempts to reach. Two cases hit a genuine local network fault mid-sweep, `getaddrinfo
failed`, DNS resolution, not the model or the harness, and I say so rather than quietly dropping
them: both crashed at step 0 before any reasoning happened, both reran clean once the connection was
back, and I'm reporting the rerun rather than the network failure because a DNS outage is not a fact
about whether this harness reasons about the Eiffel Tower correctly.

The one honest miss is worth being precise about rather than waving at. `ten-percent-brain-claim`
came back `supported` because Appraisal extracted "Humans use only 10 percent of their brains" as an
assertion from two pages that were actually debunking it, Wikipedia's own
`Ten-percent-of-the-brain_myth` article and a neuroscience-for-kids page that opens "There is no
scientific basis" for the claim. Both open by restating the myth before rejecting it, and the
extraction caught the restatement, not the rejection: it graded those assertions at the lowest
possible credibility, `CANNOT_BE_JUDGED`, which shows something was already off, but Reconciliation's
free-text reasoning then called them "high-credibility" anyway and let volume (three restatements
against one direct refutation) decide it. `goldfish-memory-claim`, the other myth case in this set,
extracted cleanly, because its sources state the true fact directly ("have a memory span longer than
three seconds") rather than restating the myth first. I found this by reading the actual retrieved
document text, not by guessing, and I'm not fixing it now: this is the holdout set, the one sweep
that has to run only once and only after every other line in this document was already frozen. Taking
it as a specific piece of future work instead of feeding it back into the prompts is the whole reason
this table means anything.

<!-- /osint-harness:tuning-summary -->

---

## Repository layout

```
src/osint_harness/
  domain/        subjects and what a verdict on each means, provenance and Admiralty grading,
                 evidence and ACH, the investigation
  graph/         the state machine, the six phases, the memory-gated briefing, reply schemas
  sources/       encyclopedia, page fetch and Tavily web search, all behind the record/replay cassette
  model/         the reasoning client, NVIDIA or OpenRouter when live, or scripted for tests
  memory/        the cross-episode archive and the publisher register
  bench/         benchmark cases, reward, failure taxonomy, run aggregation
  report/        the analyst-facing dossier and the ablation report
benchmark/       the 14 cases, 13 held-out cases kept out of tuning, and the recorded cassette
web/             the hosted demo: a FastAPI backend, document checks included, and one static page
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

That third lesson kept being true after the gate closed. A live run against the hosted demo (not the
CLI, a real visitor clicking Investigate) produced a confident `refuted` verdict at 90% for a case
whose own trap is "none", the floor case a well-documented company should pass easily. The Competing
Hypotheses table underneath it showed every single hypothesis at zero disconfirming weight, zero
supporting weight, not tested. Reconciliation's own step log said it plainly: "scored 0
evidence-hypothesis pairs." The model had written a well-reasoned paragraph citing real evidence
while returning an empty judgments array in the very same reply, and nothing caught the mismatch,
because the guard that was supposed to refuse an unsupported verdict, `has_sufficient_evidence`, only
ever checked the Admiralty-graded weight of the raw evidence gathered, never whether any of it had
actually been scored against a hypothesis. Twenty-nine well-graded, never-linked assertions cleared
that bar easily. The existing test for this exact code path had already set up the failing shape,
strong evidence, every reconciliation call discarded, and stopped at asserting the mechanical fact
(zero calls applied) without ever asserting the one thing that mattered, what the final verdict
became. Fixed by requiring a leading hypothesis to actually exist, not just enough evidence weight,
before a conclusive judgment is allowed to stand; both the extended test and a second one reproducing
the exact live shape (`calls=()` outright) fail against the old code and pass against the fix. The
offline benchmark is untouched, byte-for-byte, because the rehearsed analyst never had a leading
hypothesis to offer in the first place.
