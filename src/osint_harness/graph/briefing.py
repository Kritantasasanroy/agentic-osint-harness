from osint_harness.domain.analysis import Evidence
from osint_harness.domain.investigation import Investigation
from osint_harness.domain.provenance import Document


class Briefing:
    """What a phase is told about the investigation so far, filtered by the episode's memory mode.

    This is where the memory ablation actually happens. Under `NONE` a phase is told the subject and
    the immediately preceding phase's output and nothing else, so it cannot plan across phases.
    Under `SHORT` it also carries the episode's accumulated working state. Under `LONG` it carries
    recalled priors from earlier investigations as well, labelled as recall so they can never be
    mistaken for evidence gathered in this episode.
    """

    def __init__(self, investigation: Investigation) -> None:
        self._investigation = investigation

    def header(self) -> str:
        """The standing context every phase receives, with memory applied."""
        parts = [self._subject(), self._preceding_output()]
        if self._investigation.memory_mode.carries_working_state():
            parts.append(self._working_state())
        if self._investigation.memory_mode.recalls_past_episodes():
            parts.append(self._priors())
        return "\n\n".join(part for part in parts if part)

    def _subject(self) -> str:
        subject = self._investigation.subject
        return f"SUBJECT ({subject.kind.value}): {subject.descriptor()}"

    def _preceding_output(self) -> str:
        if not self._investigation.steps:
            return ""
        last = self._investigation.steps[-1]
        return f"PRECEDING PHASE ({last.phase.value}): {last.reason}"

    def _working_state(self) -> str:
        sections = [
            self.open_leads(),
            self.hypotheses(),
            self.evidence(),
            self._current_assessment(),
        ]
        return "\n\n".join(section for section in sections if section)

    def _priors(self) -> str:
        priors = self._investigation.recalled_priors
        if not priors:
            return ""
        lines = "\n".join(f"- {prior.text}" for prior in priors)
        return (
            "RECALLED FROM EARLIER INVESTIGATIONS (unverified recall, NOT evidence; "
            "treat each as a lead to check, and never cite it):\n" + lines
        )

    def _current_assessment(self) -> str:
        assessment = self._investigation.latest_assessment()
        return (
            f"CURRENT ASSESSMENT: {assessment.judgment.value} "
            f"({assessment.band().value}, p={assessment.probability:.2f})"
        )

    def open_leads(self) -> str:
        """The questions still outstanding."""
        leads = self._investigation.open_leads()
        if not leads:
            return ""
        lines = "\n".join(f"- [{lead.priority.value}] {lead.question}" for lead in leads)
        return "OPEN LEADS:\n" + lines

    def hypotheses(self) -> str:
        """The competing explanations, numbered so they can be referred to by position.

        Deliberately not gated by memory mode. Hypotheses state the question under investigation
        rather than accumulate findings, and without them there is no ACH matrix to score at all,
        so withholding them would not model a weaker memory — it would remove the method.
        """
        if not self._investigation.hypotheses:
            return ""
        lines = "\n".join(
            f"[{index}] {hypothesis.statement}"
            for index, hypothesis in enumerate(self._investigation.hypotheses)
        )
        return "HYPOTHESES:\n" + lines

    def visible_evidence(self) -> dict[str, Evidence]:
        """The evidence this phase is allowed to see, after the memory mode is applied.

        Under `NONE` this narrows to what the immediately preceding phase produced, because a phase
        with no working memory has no access to what earlier rounds found. This gate is the reason
        `NONE` is a real control arm rather than a label.
        """
        investigation = self._investigation
        if investigation.memory_mode.carries_working_state():
            return investigation.evidence
        if not investigation.steps:
            return {}
        produced = set(investigation.steps[-1].evidence_added)
        return {
            identifier: item
            for identifier, item in investigation.evidence.items()
            if identifier in produced
        }

    def evidence(self) -> str:
        """The visible evidence, with the identifier each item must be referred to by."""
        visible = self.visible_evidence()
        if not visible:
            return ""
        lines = "\n".join(
            f"[{identifier}] ({item.source_domain}, source grade "
            f"{self._investigation.reliability_of(item.source_domain).value}, "
            f"credibility {item.credibility.value}) {item.assertion}"
            for identifier, item in visible.items()
        )
        return "EVIDENCE GATHERED:\n" + lines

    def documents_awaiting_appraisal(self) -> tuple[Document, ...]:
        """Retrieved documents nothing has yet been extracted from."""
        cited = {item.document_url for item in self._investigation.evidence.values()}
        return tuple(
            document
            for url, document in self._investigation.documents.items()
            if url not in cited
        )

    def unappraised_text(self) -> str:
        """The documents awaiting appraisal, rendered for reading."""
        documents = self.documents_awaiting_appraisal()
        if not documents:
            return ""
        lines = "\n\n".join(
            f"URL: {document.url}\nTITLE: {document.title}\nTEXT: {document.excerpt(1200)}"
            for document in documents
        )
        return "DOCUMENTS AWAITING APPRAISAL:\n" + lines
