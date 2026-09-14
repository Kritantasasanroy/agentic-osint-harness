# Live memory ablation: `none` / `short` / `long`

6 cases (2 recall pairs plus 2 recall-free controls), each run under all three memory modes against
a live model and a live, shared recording cassette: anthropic-company, einstein-failed-maths-claim,
openai-company, einstein-nobel-relativity-claim, theranos-company, john-smith-person, in that fixed
order for every mode. `openai-company` (0.79 descriptor similarity to `anthropic-company`) and
`einstein-nobel-relativity-claim` (0.57 similarity to `einstein-failed-maths-claim`) are the two
recall pairs, run earlier-then-later so `long`'s archive gets a real chance to recall something;
`theranos-company` and `john-smith-person` are recall-free controls, since nothing else in the set
crosses the archive's 0.55 match threshold.

Retrieval here is live, not replayed from a frozen cassette the way the [offline ablation](../../runs/results/ablation.md)
is: a shared recording cassette replays a lookup an earlier episode already made, but different
modes reason differently and so ask for different things, which the earlier episodes' own memory
does not control for. A difference between rows below is therefore evidence about memory, mixed
with ordinary run-to-run variance in what each arm chose to look up, not an isolated effect the way
the byte-identical offline sweep is.

**7 of the 18 episodes (39%) halted on a provider capacity error** (429 or 503) partway through,
concentrated on the same two cases in every mode. This ran while two other live sweeps shared the
same provider allowance; every halted episode is scored as reached, not rerun, so the numbers below
carry that load artifact rather than hide it. At n=6 per arm with over a third of episodes cut short,
this is a demonstration that the instrumentation works end to end on genuine live reasoning, not a
sample large enough to claim memory helps, hurts, or is neutral beyond "no damage observed here."

### Accuracy, calibration and reward

| Memory | Accuracy | Mean probability | Mean Brier | Mean episode reward |
| --- | --- | --- | --- | --- |
| `none` | 4/6 = 67% | 0.712 | 0.128 | 0.519 |
| `short` | 5/6 = 83% | 0.833 | 0.152 | 0.644 |
| `long` | 5/6 = 83% | 0.772 | 0.091 | 0.574 |

The entire between-mode accuracy spread is one case, `openai-company`, which only `none` misses
(`short` and `long` both recall or otherwise reach the right answer). `long` posts the best Brier
score of the three despite matching `short`'s higher miss rate, i.e. it's better calibrated about
the same accuracy, not more accurate outright.

### Efficiency and cost

| Memory | Mean steps | Mean tool calls (failed) | Mean latency (s) | Total tokens |
| --- | --- | --- | --- | --- |
| `none` | 8.33 | 13.33 (19) | 198 | 98,149 |
| `short` | 8.33 | 15.33 (22) | 293 | 138,129 |
| `long` | 9.17 | 16.00 (21) | 198 | 150,401 |

Tokens and latency are provider-measured and wall-clock, not the synthetic per-call estimate the
offline sweep uses. `long` spends the most of the three, on both steps and tokens, for calibration
rather than accuracy gains at this sample size.

### Convergence

| Memory | Mean verdict changes | Mean assessments to a stable verdict |
| --- | --- | --- |
| `none` | 0.50 | 0.50 |
| `short` | 0.83 | 0.83 |
| `long` | 0.67 | 0.67 |

### Retrieval quality

| Memory | Irrelevant retrieval | Harmful retrieval | Memory-interference tag |
| --- | --- | --- | --- |
| `none` | 0% | 0% | 0 |
| `short` | 0% | 0% | 0 |
| `long` | 33% | 0% | 0 |

Irrelevant retrieval counts episodes handed a prior about a different subject; harmful retrieval
counts those where that coincided with a wrong conclusion. `long`'s one irrelevant recall
(`einstein-nobel-relativity-claim` recalling `einstein-failed-maths-claim`, 0.57 similarity) did not
produce a wrong answer or a memory-interference failure tag, only an `overconfident` calibration tag
on an otherwise correct verdict. Recall fired exactly twice in this run, both on the two designed
recall pairs, and both stayed correct.

### Reward progression

Mean cumulative step reward (evidence and retrieval credit less token and call cost) after each step.

| Step | `none` | `short` | `long` |
| --- | --- | --- | --- |
| 1 | -0.026 | -0.026 | -0.026 |
| 2 | 0.644 | 0.833 | 0.893 |
| 3 | 2.592 | 4.991 | 3.319 |
| 4 | 2.557 | 4.924 | 3.265 |
| 5 | 2.537 | 4.888 | 3.233 |
| 6 | 3.288 | 5.505 | 3.989 |
| 7 | 4.861 | 6.643 | 5.942 |
| 8 | 4.837 | 6.599 | 5.894 |
| 9 | 4.821 | 6.579 | 5.868 |
| 10 | 4.806 | 6.553 | 5.835 |

### Confidence progression

Mean stated probability after each successive assessment, from the 0.50 baseline every episode opens
with.

| Assessment | `none` | `short` | `long` |
| --- | --- | --- | --- |
| 1 | 0.50 | 0.50 | 0.50 |
| 2 | 0.73 | 0.83 | 0.76 |
| 3 | 0.71 | 0.83 | 0.77 |

### Where investigations broke down

One tag per episode, assigned by fixed priority; halted episodes stay in the denominator.

| Failure | `none` | `short` | `long` |
| --- | --- | --- | --- |
| none | 3 | 3 | 3 |
| fabricated citation | 0 | 0 | 0 |
| tool failure | 0 | 0 | 0 |
| memory interference | 0 | 0 | 0 |
| weak evidence | 2 | 0 | 1 |
| source selection | 0 | 0 | 0 |
| premature stop | 0 | 1 | 0 |
| no convergence | 1 | 2 | 1 |
| overconfident | 0 | 0 | 1 |
| underconfident | 0 | 0 | 0 |

### Per-case verdicts

| Case | Expected | `none` | `short` | `long` |
| --- | --- | --- | --- | --- |
| anthropic-company | supported | OK 0.95, halted 429 | OK 0.92, halted 429 | OK 0.82, halted 503 |
| einstein-failed-maths-claim | refuted | MISS insufficient_evidence 0.50, halted 429 | MISS partially_supported 0.80, halted 429 | MISS insufficient_evidence 0.50, halted 503 |
| openai-company | supported | MISS insufficient_evidence 0.50 | OK 0.92, halted 429 | OK 0.90, recalled anthropic-company (0.79) |
| einstein-nobel-relativity-claim | partially_supported | OK 0.90 | OK 0.90 | OK 0.95, recalled einstein-failed-maths-claim (0.57), overconfident |
| theranos-company | refuted | OK 0.92 | OK 0.96 | OK 0.96 |
| john-smith-person | insufficient_evidence | OK 0.50 | OK 0.50 | OK 0.50 |

Both Einstein misses reflect the same source-selection shortfall decision #14 in the README describes
in full for this case, not a memory effect: it fails the same way in `none`, which has no memory to
interfere with.
