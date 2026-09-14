"""Checking an uploaded document: reading its text, the claim it is checked on, and the endpoint."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import DocumentJobView, JobState, app
from documents import DocumentClaim, Intake, SubmittedDocument, UnreadableDocumentError, Verdict
from osint_harness.domain.investigation import Investigation, MemoryMode
from osint_harness.domain.subject import Claim
from osint_harness.model.client import ScriptedModel


class Upload:
    """Real files in every supported format, built in memory so no fixture sits on disk."""

    SENTENCE = "Acme Corp opened its first factory in Leeds in 2021, its annual report says."

    @classmethod
    def pdf(cls, text: str) -> bytes:
        """A minimal but valid one-page PDF whose only content draws `text` in Helvetica."""
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        bodies = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        pdf = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(bodies, start=1):
            offsets.append(len(pdf))
            pdf += b"%d 0 obj\n%s\nendobj\n" % (number, body)
        table = len(pdf)
        pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
        pdf += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(bodies) + 1,
            table,
        )
        return bytes(pdf)

    @classmethod
    def word(cls, *paragraphs: str) -> bytes:
        """A minimal Word document holding `paragraphs`."""
        runs = "".join(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{runs}</w:body></w:document>"
        )
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return buffer.getvalue()

    @classmethod
    def document(cls) -> SubmittedDocument:
        return SubmittedDocument.read("note.txt", cls.SENTENCE.encode())


class TestReadingAnUpload:
    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            ("note.txt", Upload.SENTENCE.encode()),
            ("note.md", f"# Notes\n\n{Upload.SENTENCE}".encode()),
            ("page.html", f"<p>{Upload.SENTENCE}</p><script>track()</script>".encode()),
            ("report.pdf", Upload.pdf(Upload.SENTENCE)),
            ("report.docx", Upload.word("Annual report", Upload.SENTENCE)),
        ],
    )
    def test_every_supported_format_is_read_down_to_its_text(
        self, filename: str, content: bytes
    ) -> None:
        document = SubmittedDocument.read(filename, content)

        assert Upload.SENTENCE in document.text
        assert "track()" not in document.text

    def test_a_format_it_cannot_read_is_refused_with_what_would_work(self) -> None:
        with pytest.raises(UnreadableDocumentError, match="PDF, Word"):
            SubmittedDocument.read("photo.png", b"\x89PNG")

    @pytest.mark.parametrize("filename", ["broken.pdf", "broken.docx"])
    def test_a_corrupt_file_is_refused_rather_than_raising_something_else(
        self, filename: str
    ) -> None:
        with pytest.raises(UnreadableDocumentError):
            SubmittedDocument.read(filename, b"this is not really that kind of file at all")

    def test_a_file_with_no_readable_text_is_refused(self) -> None:
        with pytest.raises(UnreadableDocumentError, match="no readable text"):
            SubmittedDocument.read("empty.html", b"<html><script>only()</script></html>")

    def test_only_the_opening_of_a_long_document_is_kept_and_the_whole_is_counted(self) -> None:
        document = SubmittedDocument.read("long.txt", ("word " * 10_000).encode())

        assert len(document.text) == SubmittedDocument.CHARACTERS_READ
        assert document.total_characters > SubmittedDocument.CHARACTERS_READ


class TestTheClaimAnUploadIsCheckedOn:
    def test_the_claim_comes_from_the_model_with_the_document_fenced_off(self) -> None:
        model = ScriptedModel()
        claim = DocumentClaim(
            checkable=True, proposition="Acme Corp opened a factory in Leeds in 2021."
        )
        model.script("intake", claim)

        read = Intake(model).claim_of(Upload.document())

        assert read == claim
        assert f"<<<DOCUMENT\n{Upload.SENTENCE}\nDOCUMENT>>>" in model.prompts_seen[0]

    def test_a_checkable_claim_becomes_a_subject_named_for_its_file(self) -> None:
        claim = DocumentClaim(
            checkable=True, proposition="  Acme Corp opened a factory in Leeds in 2021. "
        )

        subject = claim.subject(Upload.document())

        assert subject is not None
        assert subject.name == "note.txt"
        assert subject.proposition_under_test() == "Acme Corp opened a factory in Leeds in 2021."

    @pytest.mark.parametrize(
        "claim",
        [
            DocumentClaim(checkable=False, reason="an opinion column"),
            DocumentClaim(checkable=True, proposition="   "),
        ],
    )
    def test_nothing_is_investigated_when_there_is_nothing_to_check(
        self, claim: DocumentClaim
    ) -> None:
        assert claim.subject(Upload.document()) is None
        assert "no investigation was run" in claim.refusal_report(Upload.document())


class TestADocumentCheckResult:
    def _finished(
        self, claim: DocumentClaim, investigation: Investigation | None
    ) -> DocumentJobView:
        return DocumentJobView(
            id="j1",
            state=JobState.DONE,
            phase="complete",
            calls_made=3,
            elapsed_seconds=9.0,
            document=Upload.document(),
            claim=claim,
            verdict=Verdict.of(investigation) if investigation is not None else None,
            record=investigation,
            markdown="report",
        )

    def test_it_carries_a_verdict_exactly_when_its_claim_was_checkable(self) -> None:
        investigation = Investigation.open(
            run_id="r1",
            subject=Claim(name="note.txt", proposition="Acme opened a factory."),
            memory_mode=MemoryMode.SHORT,
        )
        checkable = DocumentClaim(checkable=True, proposition="Acme opened a factory.")
        opinion = DocumentClaim(checkable=False, reason="an opinion column")

        assert self._finished(checkable, investigation).verdict is not None
        assert self._finished(opinion, None).verdict is None
        with pytest.raises(ValidationError):
            self._finished(checkable, None)
        with pytest.raises(ValidationError):
            self._finished(opinion, investigation)


class TestTheUploadEndpoint:
    def test_an_unreadable_upload_is_refused_before_any_job_or_model_call(self) -> None:
        response = TestClient(app).post(
            "/api/documents", params={"filename": "photo.png"}, content=b"\x89PNG"
        )

        assert response.status_code == 422
        assert "PDF" in response.json()["detail"]

    def test_an_upload_past_the_size_limit_is_refused(self) -> None:
        response = TestClient(app).post(
            "/api/documents",
            params={"filename": "huge.txt"},
            content=b"a" * (SubmittedDocument.MAX_BYTES + 1),
        )

        assert response.status_code == 413
