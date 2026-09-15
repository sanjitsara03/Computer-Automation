"""Operator handoff: the mechanism is real, the terminal is the mock operator surface."""

from __future__ import annotations

from typing import Literal, Protocol

from app.schema import Strict


class InterventionRequest(Strict):
    capability: str
    step: str
    reason: str
    expected: str | None = None
    observed: str | None = None
    screenshot: str | None = None


class InterventionOutcome(Strict):
    decision: Literal["approved", "fixed", "abort"]
    note: str = ""


class OperatorChannel(Protocol):
    def request(self, req: InterventionRequest) -> InterventionOutcome: ...


class TerminalOperator:
    """Prints the request; the human acts in the open browser, then answers here."""

    def request(self, req: InterventionRequest) -> InterventionOutcome:
        print("\n=== INTERVENTION REQUIRED ===")
        print(f"capability: {req.capability}   step: {req.step}")
        print(f"reason:   {req.reason}")
        if req.expected:
            print(f"expected: {req.expected}")
        if req.observed:
            print(f"observed: {req.observed}")
        if req.screenshot:
            print(f"screenshot: {req.screenshot}")
        print("the live browser window is yours; act there if needed, then answer")
        while True:
            answer = input("decision [approve / fixed / abort] optional note: ").strip()
            word, _, note = answer.partition(" ")
            decisions = {"approve": "approved", "approved": "approved",
                         "fixed": "fixed", "abort": "abort"}
            if word.lower() in decisions:
                return InterventionOutcome(decision=decisions[word.lower()], note=note.strip())
            print("answer must start with approve, fixed, or abort")
