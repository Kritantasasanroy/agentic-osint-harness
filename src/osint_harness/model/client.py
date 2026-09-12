from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field


class ModelRefusedError(Exception):
    """Raised when the model declined, so the episode fails visibly rather than silently."""


class ModelUnavailableError(Exception):
    """Raised when no reply could be obtained, so a failed episode is tagged rather than hidden."""


class Usage(BaseModel):
    """Tokens consumed by model calls, which is how the efficiency metric is paid for."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    def total(self) -> int:
        """Every token spent, in and out."""
        return self.input_tokens + self.output_tokens

    def plus(self, other: "Usage") -> "Usage":
        """The combined cost of two calls."""
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )

    def since(self, earlier: "Usage") -> "Usage":
        """What has been spent since an earlier reading, for per-step accounting."""
        return Usage(
            input_tokens=self.input_tokens - earlier.input_tokens,
            output_tokens=self.output_tokens - earlier.output_tokens,
        )


class ModelClient(ABC):
    """The reasoning engine behind each phase, and the meter that records what it cost.

    Its only responsibility is turning a prompt into a structured decision. Gathering information
    — search included — is a `Tool`, never a model capability: that keeps every retrieval, search
    among them, behind the same cassette and the same failure handling, and keeps the harness's
    ability to look things up independent of which reasoning provider is behind `decide()`.
    """

    def __init__(self) -> None:
        self._spent = Usage()

    def spent(self) -> Usage:
        """Everything this client has consumed so far. Read either side of a phase for its cost."""
        return self._spent

    def _charge(self, usage: Usage) -> None:
        """Add one call's cost to the running meter."""
        self._spent = self._spent.plus(usage)

    @abstractmethod
    def decide[T: BaseModel](self, purpose: str, system: str, prompt: str, schema: type[T]) -> T:
        """Answer a phase's question in the exact shape that phase requires."""


class ScriptedModel(ModelClient):
    """A model whose every answer is fixed in advance, so runs are deterministic and free.

    This is what makes the harness runnable with no API key and what lets the engine, the nodes and
    the metrics be tested without paying for or depending on a live model.
    """

    def __init__(
        self,
        decisions: dict[str, BaseModel] | None = None,
        cost_per_call: Usage | None = None,
    ) -> None:
        super().__init__()
        self._decisions = dict(decisions) if decisions is not None else {}
        self._cost = cost_per_call if cost_per_call is not None else Usage(
            input_tokens=100, output_tokens=50
        )
        self.purposes_asked: list[str] = []
        self.prompts_seen: list[str] = []

    def script(self, purpose: str, reply: BaseModel) -> None:
        """Fix the answer this model will give for one phase."""
        self._decisions[purpose] = reply

    def decide[T: BaseModel](self, purpose: str, system: str, prompt: str, schema: type[T]) -> T:
        self.purposes_asked.append(purpose)
        self.prompts_seen.append(f"{system}\n{prompt}")
        self._charge(self._cost)
        if purpose not in self._decisions:
            raise ModelUnavailableError(f"no scripted reply for {purpose!r}")
        reply = self._decisions[purpose]
        if not isinstance(reply, schema):
            raise ModelUnavailableError(
                f"scripted reply for {purpose!r} is {type(reply).__name__}, "
                f"but {schema.__name__} was required"
            )
        return reply
