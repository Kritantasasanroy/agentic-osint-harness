# A real live run, kept as evidence

I ran one real investigation with `--live --record`, against OpenRouter's free
`nex-agi/nex-n2.5-pro:free` model. Not the rehearsed analyst standing in, not a mock, the real thing.
I'm keeping the output here because the rest of the submission's benchmark numbers come from the
deterministic stand-in (the README's "Honest status" section explains why), and anyone reviewing
this deserves to see what the real reasoning path actually produces, without needing their own API
key to check.

- **`findings.md`**: the rendered dossier, exactly as `osint-harness investigate` printed it out.
- **`investigation.json`**: the full record underneath that. Every step, every piece of evidence,
  every hypothesis and its ACH scores, every token spent along the way.

## What actually happened

The episode ran `ada-lovelace-person` under `MemoryMode.SHORT`. Direction, Collection, Appraisal, and
Reconciliation all completed in full: real search, real Wikipedia retrieval, 18 assertions pulled and
graded from actual page text, 72 evidence-hypothesis judgments scored through Analysis of Competing
Hypotheses, and a `supported` verdict at 0.78 probability, with reasoning that specifically names
which hypothesis survived which disconfirming piece of evidence. Reflection then looped back into
Collection for a second pass, and that call blew past the model's token ceiling on a heavier,
evidence-laden prompt. The harness's crash-tolerant halt caught it cleanly instead of taking the
process down, which is exactly what that path was built to do.

Two things worth knowing before you open `investigation.json` yourself:

1. **The source reliability grades look emptier than the run actually was.** Appraisal's first pass
   graded both publishers with real, specific reasoning (it graded MacTutor a `B` because it's "a
   specialist history-of-mathematics resource... though the supplied excerpt does not expose enough
   evidentiary detail to justify an A"), but wrote the domain field as the hostname plus a
   description, not a bare hostname. So, as originally shipped, that grading got stored under a key
   the evidence never actually looked up. Check the README's decision log and
   `tests/test_regressions.py::TestAGradingSurvivesWhateverTheModelWritesInDomain` for the fix this
   exact run led to.
2. **`source_grades` in the JSON is a superset of what the dossier shows.** It recorded both the
   mismatched keys from that first grading attempt and the clean ones that actually matched evidence.
   I captured this file before the fix landed, and that's exactly why I kept it instead of
   regenerating it: it's primary evidence of the bug, not just an example of the harness working.
