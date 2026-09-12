# A real live run, kept as evidence

This directory holds the unedited output of one real investigation, run with `--live --record`
against OpenRouter's free `nex-agi/nex-n2.5-pro:free` model — not the rehearsed analyst, and not a
mock. It is kept here because the rest of the submission's benchmark numbers come from the
deterministic stand-in (see the README's "Honest status" section for why), and a reviewer should be
able to see what the real reasoning path actually produces without needing their own API key.

- **`findings.md`** — the rendered dossier, exactly as `osint-harness investigate` printed it.
- **`investigation.json`** — the full underlying record: every step, every piece of evidence, every
  hypothesis and its ACH scores, every token spent.

## What happened

The episode ran `ada-lovelace-person` under `MemoryMode.SHORT`. Direction, Collection, Appraisal and
Reconciliation completed in full: real search and Wikipedia retrieval, 18 assertions extracted and
graded from real page text, 72 evidence-hypothesis judgments scored by Analysis of Competing
Hypotheses, and a `supported` verdict at 0.78 probability with reasoning that specifically cites
which hypothesis survived which disconfirming evidence. Reflection then looped back to Collection
for a second pass, and that call exceeded the model's token ceiling on a heavier, evidence-laden
prompt — caught cleanly by the harness's crash-tolerant halt rather than crashing the process, which
is exactly the behaviour that path was built to have.

Two things worth knowing before reading `investigation.json` directly:

1. **Source reliability grades look emptier than the run actually was.** Appraisal's *first* pass
   graded both publishers with real, specific reasoning (e.g. grading MacTutor `B` because it is "a
   specialist history-of-mathematics resource... though the supplied excerpt does not expose enough
   evidentiary detail to justify an A"), but wrote the domain as the hostname followed by a
   description rather than a bare hostname, so — as originally shipped — that grading was stored
   under a key the evidence never looked up. See the README's decision log and
   `tests/test_regressions.py::TestAGradingSurvivesWhateverTheModelWritesInDomain` for the fix this
   run led to.
2. **`source_grades` in the JSON is a superset of what the dossier shows**, because it recorded both
   the mismatched keys from that first grading attempt and the clean ones matching evidence. This
   file was captured before the fix landed, which is exactly why it is worth keeping rather than
   regenerating: it is primary evidence for the bug, not just an example of the harness working.
