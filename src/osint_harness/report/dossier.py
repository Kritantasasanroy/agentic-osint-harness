from osint_harness.domain.investigation import Investigation


class Dossier:
    """A finished investigation as an analyst receives it.

    Every claim in the rendered report traces to a citation, every citation to a document the
    episode actually retrieved, and every source carries the grade that was applied to it at the
    time. A reader who distrusts the conclusion can follow it back to what it rests on.
    """

    def __init__(self, investigation: Investigation) -> None:
        self._investigation = investigation

    def as_markdown(self) -> str:
        """The report, written for a human analyst.

        Deliberately does not re-check that citations are grounded. `Investigation` refuses to be
        constructed or reloaded holding an unbacked citation, and Dissemination refuses to conclude
        on one, so a renderer repeating the check would be re-litigating a decision two boundaries
        above it have already made.
        """
        sections = [
            self._heading(),
            self._verdict(),
            self._summary(),
            self._key_findings(),
            self._hypotheses(),
            self._evidence(),
            self._conflicts(),
            self._gaps(),
            self._trajectory(),
        ]
        return "\n\n".join(section for section in sections if section) + "\n"

    def as_json(self) -> str:
        """The full investigation record, from which every number in the report is recomputable."""
        return self._investigation.model_dump_json(indent=2)

    def _heading(self) -> str:
        subject = self._investigation.subject
        return f"# Findings — {subject.descriptor()}\n\n_Subject kind: {subject.kind.value}_"

    def _verdict(self) -> str:
        investigation = self._investigation
        assessment = investigation.latest_assessment()
        low, high = assessment.band().probability_range()
        return "\n".join(
            [
                "## Verdict",
                "",
                f"- **Judgment:** {assessment.judgment.value.replace('_', ' ')}",
                f"- **Confidence:** {assessment.band().value} "
                f"({low:.0%} to {high:.0%}), stated {assessment.probability:.2f}",
                f"- **Leading hypothesis:** {assessment.leading_hypothesis or 'none recorded'}",
                f"- **Reasoning:** {assessment.rationale or 'none recorded'}",
                "",
                f"Run `{investigation.run_id}` · memory `{investigation.memory_mode.value}` · "
                f"{len(investigation.steps)} steps · "
                f"{len(investigation.evidence)} pieces of evidence from "
                f"{investigation.source_diversity()} sources · "
                f"{len(investigation.tool_calls())} lookups · "
                f"{investigation.token_cost()} tokens · "
                f"verdict changed {investigation.verdict_changes()} times, "
                f"settling after assessment {investigation.assessments_to_stable_verdict()} "
                f"of {len(investigation.assessments)}",
            ]
        )

    def _summary(self) -> str:
        summary = self._investigation.findings.summary
        if not summary:
            return ""
        return f"## Summary\n\n{summary}"

    def _key_findings(self) -> str:
        findings = self._investigation.findings.key_findings
        if not findings:
            return ""
        lines = "\n".join(f"- {finding}" for finding in findings)
        return f"## Key findings\n\n{lines}"

    def _hypotheses(self) -> str:
        investigation = self._investigation
        if not investigation.hypotheses:
            return ""
        weights = investigation.evidence_weights()
        rows = "\n".join(
            f"| {position + 1} | {hypothesis.statement} | "
            f"{hypothesis.inconsistency_score(weights):.2f} | "
            f"{hypothesis.support_score(weights):.2f} |"
            for position, hypothesis in enumerate(investigation.ranked_hypotheses())
        )
        return "\n".join(
            [
                "## Competing hypotheses",
                "",
                "Ranked by Analysis of Competing Hypotheses: the least disconfirmed comes first, "
                "because evidence that rules an explanation out is worth more than evidence "
                "consistent with several.",
                "",
                "| Rank | Hypothesis | Disconfirming weight | Supporting weight |",
                "| --- | --- | --- | --- |",
                rows,
            ]
        )

    def _evidence(self) -> str:
        investigation = self._investigation
        if not investigation.evidence:
            return "## Evidence\n\nNo evidence was gathered."
        rows = "\n".join(
            f"| `{identifier}` | {item.assertion} | {item.source_domain} | "
            f"{self._grade_of(item.source_domain)} | {item.credibility.value} | "
            f"[source]({item.document_url}) |"
            for identifier, item in investigation.evidence.items()
        )
        return "\n".join(
            [
                "## Evidence",
                "",
                "Graded on the Admiralty scale: source reliability A-F grades the publisher, "
                "information credibility 1-6 grades the individual claim. They are separate "
                "judgments. Each grade carries the reason it was given, because a bare letter "
                "with nothing behind it is a decoration rather than an assessment.",
                "",
                "| ID | Assertion | Publisher | Reliability | Credibility | Citation |",
                "| --- | --- | --- | --- | --- | --- |",
                rows,
            ]
        )

    def _grade_of(self, domain: str) -> str:
        source = self._investigation.source_for(domain)
        return f"{source.reliability.value} — {source.reason}"

    def _conflicts(self) -> str:
        conflicts = self._investigation.findings.conflicts
        if not conflicts:
            return ""
        lines = "\n".join(f"- {conflict}" for conflict in conflicts)
        return f"## Where sources disagree\n\n{lines}"

    def _gaps(self) -> str:
        investigation = self._investigation
        gaps = list(investigation.findings.gaps)
        gaps.extend(f"Unanswered: {lead.question}" for lead in investigation.open_leads())
        if not gaps:
            return ""
        lines = "\n".join(f"- {gap}" for gap in gaps)
        return f"## What remains unknown\n\n{lines}"

    def _trajectory(self) -> str:
        investigation = self._investigation
        if not investigation.steps:
            return ""
        rows = "\n".join(
            f"| {step.index} | {step.phase.value} | {step.moved_to.value} | {step.reason} | "
            f"{len(step.tool_calls)} | {step.tokens()} |"
            for step in investigation.steps
        )
        return "\n".join(
            [
                "## Investigation log",
                "",
                "| Step | Phase | Moved to | Why | Lookups | Tokens |",
                "| --- | --- | --- | --- | --- | --- |",
                rows,
            ]
        )
