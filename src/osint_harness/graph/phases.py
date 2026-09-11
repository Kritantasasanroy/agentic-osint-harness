import re
from abc import abstractmethod
from time import perf_counter
from typing import ClassVar

import httpx

from osint_harness.domain.analysis import Assessment, Evidence, Findings, Hypothesis, Judgment
from osint_harness.domain.investigation import (
    Investigation,
    InvestigationPhase,
    Lead,
    ToolCall,
    UngroundedEvidenceError,
)
from osint_harness.domain.provenance import Source
from osint_harness.graph.briefing import Briefing
from osint_harness.graph.machine import Node, Transition
from osint_harness.graph.schemas import (
    AppraisalResult,
    CollectionPlan,
    DirectionPlan,
    ReadingChoice,
    ReconciliationResult,
    ReflectionResult,
    ReportDraft,
)
from osint_harness.model.client import ModelClient, ModelUnavailableError, SearchResult
from osint_harness.sources.cassette import CassetteMissError
from osint_harness.sources.tools import Tool


class Phase(Node):
    """A stage of the intelligence cycle, metered so its token cost lands on the step."""

    def __init__(self, model: ModelClient) -> None:
        self._model = model

    @classmethod
    def analyst_brief(cls) -> str:
        """The standing instruction every phase works under."""
        return (
            "You are an open-source intelligence analyst. Your report will be acted on, so it must "
            "be defensible.\n"
            "- Grade a publisher's track record on the Admiralty scale A-F, and each individual "
            "claim's credibility 1-6. These are separate judgments and must not be conflated.\n"
            "- Weigh competing hypotheses by Analysis of Competing Hypotheses: prefer the "
            "hypothesis with the least disconfirming evidence, not the one with the most "
            "supporting evidence.\n"
            "- You have no private knowledge. Never assert anything that a document retrieved in "
            "this investigation does not support. Something you believe but cannot cite is a lead "
            "to check, never a finding.\n"
            "- Concluding that the evidence is insufficient is always available to you and is the "
            "correct answer when the evidence does not carry a verdict. A confident wrong answer "
            "is far worse than an honest unresolved one.\n"
            "- Report contradictions as contradictions. Never average them away, and never prefer "
            "the more recent source just because it is more recent."
        )

    def advance(self, investigation: Investigation) -> Transition:
        before = self._model.spent()
        transition = self.conduct(investigation)
        spent = self._model.spent().since(before)
        return transition.model_copy(
            update={
                "input_tokens": spent.input_tokens,
                "output_tokens": spent.output_tokens,
            }
        )

    @abstractmethod
    def conduct(self, investigation: Investigation) -> Transition:
        """Do this phase's work. Token accounting is added by `advance`."""


class Direction(Phase):
    """Opens the investigation: the questions to answer, and the competing explanations."""

    MINIMUM_HYPOTHESES = 2

    def conduct(self, investigation: Investigation) -> Transition:
        subject = investigation.subject
        prompt = (
            f"{Briefing(investigation).header()}\n\n"
            "Open this investigation. Propose the lines of enquiry worth pursuing, and at least "
            "two competing hypotheses that could each turn out to be the true one.\n\n"
            "Suggested starting questions:\n"
            + "\n".join(f"- {question}" for question in subject.seed_leads())
            + "\n\nCandidate hypotheses:\n"
            + "\n".join(f"- {statement}" for statement in subject.opening_hypotheses())
        )
        plan = self._model.decide("direction", self.analyst_brief(), prompt, DirectionPlan)

        for planned in plan.leads:
            investigation.leads.append(
                Lead(question=planned.question, priority=planned.priority, origin="direction")
            )
        if not investigation.leads:
            for question in subject.seed_leads():
                investigation.leads.append(Lead(question=question, origin="direction"))

        offered = plan.hypotheses
        statements = (
            offered if len(offered) >= self.MINIMUM_HYPOTHESES else subject.opening_hypotheses()
        )
        for statement in statements:
            investigation.hypotheses.append(Hypothesis(statement=statement))

        return Transition(
            next_phase=InvestigationPhase.COLLECTION,
            reason=(
                f"opened with {len(investigation.hypotheses)} competing hypotheses and "
                f"{len(investigation.leads)} leads"
            ),
        )


class Collection(Phase):
    """Goes and looks: searches, reads, and brings documents back."""

    MAX_SEARCHES = 3
    MAX_LOOKUPS = 2
    MAX_READS = 4

    def __init__(self, model: ModelClient, encyclopedia: Tool, pages: Tool) -> None:
        super().__init__(model)
        self._encyclopedia = encyclopedia
        self._pages = pages

    def conduct(self, investigation: Investigation) -> Transition:
        briefing = Briefing(investigation)
        prompt = (
            f"{briefing.header()}\n\n"
            "Decide where to look next. Give search queries for the open questions, encyclopedia "
            "topics for background or for telling apart people who share a name, and any specific "
            "URLs already worth reading."
        )
        plan = self._model.decide("collection", self.analyst_brief(), prompt, CollectionPlan)

        calls: list[ToolCall] = []
        for topic in plan.encyclopedia_lookups[: self.MAX_LOOKUPS]:
            calls.append(self._retrieve(self._encyclopedia, topic, investigation))

        hits: list[SearchResult] = []
        for query in plan.search_queries[: self.MAX_SEARCHES]:
            call, found = self._search(query)
            calls.append(call)
            hits.extend(found)

        for url in self._urls_to_read(investigation, plan.urls_to_read, hits):
            calls.append(self._retrieve(self._pages, url, investigation))

        for lead in investigation.open_leads():
            lead.pursue()

        retrieved = sum(call.documents_returned for call in calls)
        failed = sum(1 for call in calls if not call.succeeded)
        reason = f"retrieved {retrieved} documents from {len(calls)} lookups"
        if failed:
            reason += f", {failed} of which failed"
        return Transition(
            next_phase=InvestigationPhase.APPRAISAL,
            reason=reason,
            tool_calls=tuple(calls),
        )

    def _urls_to_read(
        self,
        investigation: Investigation,
        proposed: tuple[str, ...],
        hits: list[SearchResult],
    ) -> tuple[str, ...]:
        """Pick what to actually open. Choosing badly here is a source-selection failure."""
        chosen = list(proposed)
        if hits:
            listing = "\n".join(f"- {hit.url} — {hit.title}" for hit in hits)
            choice = self._model.decide(
                "reading_choice",
                self.analyst_brief(),
                (
                    f"{Briefing(investigation).header()}\n\n"
                    f"These search results came back:\n{listing}\n\n"
                    "Choose which to open. Prefer primary sources and publishers with a track "
                    "record over aggregators and content farms."
                ),
                ReadingChoice,
            )
            chosen.extend(choice.urls)
        seen: dict[str, None] = {}
        for url in chosen:
            if url not in investigation.documents:
                seen[url] = None
        return tuple(seen)[: self.MAX_READS]

    def _search(self, query: str) -> tuple[ToolCall, tuple[SearchResult, ...]]:
        """Search, recording a failed search as a failure rather than as an empty success."""
        started = perf_counter()
        try:
            findings = self._model.search(query)
        except ModelUnavailableError as failure:
            return (
                ToolCall(
                    tool="web_search",
                    query=query,
                    documents_returned=0,
                    succeeded=False,
                    latency_seconds=perf_counter() - started,
                    failure_reason=str(failure),
                ),
                (),
            )
        return (
            ToolCall(
                tool="web_search",
                query=query,
                documents_returned=len(findings.results),
                succeeded=True,
                latency_seconds=perf_counter() - started,
            ),
            findings.results,
        )

    def _retrieve(self, tool: Tool, query: str, investigation: Investigation) -> ToolCall:
        """Fetch through one source, recording a dead source rather than letting it end the run."""
        started = perf_counter()
        try:
            documents = tool.gather(query)
        except (httpx.HTTPError, CassetteMissError) as failure:
            return ToolCall(
                tool=tool.name,
                query=query,
                documents_returned=0,
                succeeded=False,
                latency_seconds=perf_counter() - started,
                failure_reason=str(failure),
            )
        for document in documents:
            investigation.record_document(document)
        return ToolCall(
            tool=tool.name,
            query=query,
            documents_returned=len(documents),
            succeeded=True,
            latency_seconds=perf_counter() - started,
        )


class Appraisal(Phase):
    """Reads what was retrieved and turns it into graded evidence, or refuses it."""

    def conduct(self, investigation: Investigation) -> Transition:
        briefing = Briefing(investigation)
        pending = briefing.unappraised_text()
        if not pending:
            return Transition(
                next_phase=InvestigationPhase.RECONCILIATION,
                reason="no new documents to appraise",
            )

        prompt = (
            f"{briefing.header()}\n\n{pending}\n\n"
            "Extract the specific assertions these documents make that bear on the question. "
            "Every assertion must quote a URL from the list above. Grade each publisher's "
            "reliability A-F and each assertion's credibility 1-6, with your reason."
        )
        result = self._model.decide("appraisal", self.analyst_brief(), prompt, AppraisalResult)

        for grading in result.gradings:
            investigation.grade_source(
                grading.domain, grading.reliability, grading.reason or "no reason recorded"
            )

        recorded: list[str] = []
        ungrounded = 0
        for assertion in result.assertions:
            evidence = Evidence(
                assertion=assertion.assertion,
                document_url=assertion.document_url,
                source_domain=Source.registrable_domain(assertion.document_url),
                credibility=assertion.credibility,
                rationale=assertion.rationale,
            )
            try:
                recorded.append(investigation.record_evidence(evidence))
            except UngroundedEvidenceError:
                ungrounded += 1

        reason = f"extracted {len(recorded)} graded assertions"
        if ungrounded:
            reason += f"; refused {ungrounded} citing documents that were never retrieved"
        return Transition(
            next_phase=InvestigationPhase.RECONCILIATION,
            reason=reason,
            evidence_added=tuple(recorded),
        )


class Reconciliation(Phase):
    """Scores the evidence against every hypothesis and records where that leaves the question."""

    def conduct(self, investigation: Investigation) -> Transition:
        briefing = Briefing(investigation)
        prompt = (
            f"{briefing.header()}\n\n{briefing.hypotheses()}\n\n{briefing.evidence()}\n\n"
            "For each piece of evidence, say whether it is consistent with, inconsistent with, or "
            "not applicable to each hypothesis, referring to hypotheses by their number and "
            "evidence by its identifier. Then give your judgment and the probability that your "
            "judgment is correct."
        )
        result = self._model.decide(
            "reconciliation", self.analyst_brief(), prompt, ReconciliationResult
        )

        applied = 0
        for call in result.calls:
            if call.hypothesis_index >= len(investigation.hypotheses):
                continue
            if call.evidence_id not in investigation.evidence:
                continue
            investigation.hypotheses[call.hypothesis_index].judge(
                call.evidence_id, call.consistency
            )
            applied += 1

        ranked = investigation.ranked_hypotheses()
        leading = ranked[0].statement if ranked else ""
        judgment, probability = self._defensible(investigation, result)
        investigation.assess(
            Assessment(
                judgment=judgment,
                leading_hypothesis=leading,
                probability=probability,
                rationale=result.rationale,
                made_in_phase=InvestigationPhase.RECONCILIATION.value,
            )
        )
        return Transition(
            next_phase=InvestigationPhase.REFLECTION,
            reason=f"scored {applied} evidence-hypothesis pairs; judgment {judgment.value}",
        )

    def _defensible(
        self, investigation: Investigation, result: ReconciliationResult
    ) -> tuple[Judgment, float]:
        """Refuse a conclusive verdict the gathered evidence cannot actually carry."""
        if not investigation.has_sufficient_evidence():
            return (Judgment.INSUFFICIENT_EVIDENCE, min(result.probability, 0.5))
        return (result.judgment, result.probability)


class Reflection(Phase):
    """Criticises the investigation so far and decides whether concluding would be honest."""

    RESERVE_STEPS = 2

    def conduct(self, investigation: Investigation) -> Transition:
        briefing = Briefing(investigation)
        prompt = (
            f"{briefing.header()}\n\n"
            "Criticise this investigation before it concludes. What would a sceptical reviewer "
            "say is missing? Is any hypothesis being favoured on thin evidence? Is there an "
            "explanation nobody has stated yet? Only say you are ready to conclude if the "
            "evidence genuinely carries the judgment."
        )
        result = self._model.decide("reflection", self.analyst_brief(), prompt, ReflectionResult)

        for planned in result.new_leads:
            investigation.leads.append(
                Lead(question=planned.question, priority=planned.priority, origin="reflection")
            )
        for statement in result.new_hypotheses:
            investigation.hypotheses.append(
                Hypothesis(statement=statement, origin="reflection")
            )

        if self._running_out(investigation):
            return Transition(
                next_phase=InvestigationPhase.DISSEMINATION,
                reason="budget nearly spent; reporting on what is actually held",
            )
        if result.ready_to_conclude and not investigation.has_blocking_leads():
            return Transition(
                next_phase=InvestigationPhase.DISSEMINATION,
                reason=result.reason or "evidence judged sufficient to conclude",
            )
        return Transition(
            next_phase=InvestigationPhase.COLLECTION,
            reason=(
                f"{len(investigation.open_leads())} leads still open; "
                f"{len(result.gaps)} gaps identified"
            ),
        )

    def _running_out(self, investigation: Investigation) -> bool:
        """Whether there is only enough budget left to write the report."""
        return len(investigation.steps) + self.RESERVE_STEPS >= investigation.budget.max_steps


class Dissemination(Phase):
    """Writes the findings report, after proving every citation traces to a retrieved document."""

    LINK = re.compile(r"https?://[^\s)\]]+")
    TRAILING_PUNCTUATION: ClassVar[str] = ".,;:)]"

    def conduct(self, investigation: Investigation) -> Transition:
        unbacked = investigation.ungrounded_citations()
        if unbacked:
            raise UngroundedEvidenceError(
                f"refusing to report: {len(unbacked)} citations have no retrieved document behind "
                f"them ({', '.join(unbacked[:3])})"
            )

        briefing = Briefing(investigation)
        assessment = investigation.latest_assessment()
        prompt = (
            f"{briefing.header()}\n\n{briefing.hypotheses()}\n\n{briefing.evidence()}\n\n"
            f"The standing judgment is {assessment.judgment.value} at "
            f"{assessment.band().value} confidence.\n\n"
            "Write the findings report an analyst would act on. State what was established and "
            "what it rests on, what remains unknown, and where sources contradict each other. "
            "Do not state anything the evidence above does not support."
        )
        draft = self._model.decide("dissemination", self.analyst_brief(), prompt, ReportDraft)

        invented = self.unbacked_links(draft, investigation)
        if invented:
            raise UngroundedEvidenceError(
                f"refusing to report: the narrative cites {len(invented)} links no document "
                f"backs ({', '.join(invented[:3])})"
            )

        investigation.record_findings(
            Findings(
                summary=draft.summary,
                key_findings=draft.key_findings,
                gaps=draft.gaps,
                conflicts=draft.conflicts,
            )
        )
        return Transition(
            next_phase=InvestigationPhase.COMPLETE,
            reason=(
                f"reported {len(draft.key_findings)} findings on "
                f"{len(investigation.evidence)} pieces of evidence from "
                f"{investigation.source_diversity()} sources"
            ),
        )

    @classmethod
    def unbacked_links(
        cls, draft: ReportDraft, investigation: Investigation
    ) -> tuple[str, ...]:
        """Links the narrative introduced that no retrieved document stands behind.

        The evidence table is grounded by `record_evidence`, but the written narrative is free text
        and could otherwise smuggle in a citation nothing supports. This catches an invented link;
        it cannot catch an invented sentence carrying no link, which is stated as a limitation
        rather than papered over.
        """
        written = " ".join(
            [draft.summary, *draft.key_findings, *draft.gaps, *draft.conflicts]
        )
        return tuple(
            link
            for link in (
                found.rstrip(cls.TRAILING_PUNCTUATION) for found in cls.LINK.findall(written)
            )
            if link not in investigation.documents
        )
