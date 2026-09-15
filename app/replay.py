"""Deterministic replay: execute a recorded artifact with no model in the loop."""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import Literal, Union

from playwright.sync_api import Page, expect, sync_playwright

from app.operator import InterventionRequest, OperatorChannel, TerminalOperator
from app.policy import Policy, check_action, check_origin, load_policy
from app.resolve import resolve
from app.schema import (
    Artifact,
    Checkpoint,
    LiteralValue,
    ParamRef,
    RoleVisible,
    SecretRef,
    Step,
    Strict,
    TextMatches,
    UrlMatches,
    Value,
    ValueMatches,
    load_artifact,
)

#result contract
class StepResult(Strict):
    id: str
    action: str
    ok: bool
    ms: int
    tier: str | None = None

class Success(Strict):
    kind: Literal["success"] = "success"

class BusinessOutcome(Strict):
    """The app gave a legitimate 'no' the caller needs to know about."""
    kind: Literal["business"] = "business"
    name: str
    step: str

class HardFailure(Strict):
    kind: Literal["hard_failure"] = "hard_failure"
    step: str
    expected: str
    observed: str
    screenshot: str | None = None

Outcome = Union[Success, BusinessOutcome, HardFailure]

class InterventionRecord(Strict):
    step: str
    reason: str
    decision: str
    note: str = ""

class ReplayResult(Strict):
    ok: bool
    capability: str
    outcome: Outcome
    steps: list[StepResult]
    outputs: dict[str, str] = {}
    interventions: list[InterventionRecord] = []

#session
class Session:
    """Owns the live browser session so a human can take it over later."""

    def __init__(self, base_url: str, headless: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.page: Page | None = None
        self._pw = None
        self._browser = None

    def __enter__(self) -> "Session":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.page = self._browser.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()


#execution
def bind_value(value: Value, params: dict[str, str], secrets: dict[str, str]) -> str:
    if isinstance(value, LiteralValue):
        return value.literal
    if isinstance(value, ParamRef):
        if value.param not in params:
            raise KeyError(f"param not provided: {value.param}")
        return params[value.param]
    if isinstance(value, SecretRef):
        if value.secret not in secrets:
            raise KeyError(f"secret not provided: {value.secret}")
        return secrets[value.secret]
    raise NotImplementedError(f"unknown value type: {type(value).__name__}")


def assert_checkpoint(
    page: Page,
    checkpoint: Checkpoint,
    timeout_ms: int,
    locator=None,
    value: str | None = None,
) -> None:
    if isinstance(checkpoint, RoleVisible):
        expect(page.get_by_role(checkpoint.role, name=checkpoint.name)).to_be_visible(
            timeout=timeout_ms
        )
    elif isinstance(checkpoint, TextMatches):
        expect(page.locator("body")).to_contain_text(
            re.compile(checkpoint.pattern), timeout=timeout_ms
        )
    elif isinstance(checkpoint, UrlMatches):
        expect(page).to_have_url(re.compile(checkpoint.pattern), timeout=timeout_ms)
    elif isinstance(checkpoint, ValueMatches):
        if locator is None or value is None:
            raise ValueError("value_matches requires the acted-on locator and value")
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            selected = locator.evaluate("el => el.options[el.selectedIndex].text")
            if selected != value:
                raise AssertionError(f"selected option {selected!r}, expected {value!r}")
        else:
            expect(locator).to_have_value(value, timeout=timeout_ms)
    else:
        raise NotImplementedError(f"checkpoint not implemented: {type(checkpoint).__name__}")


def run_step(
    session: Session, step: Step, params: dict[str, str], secrets: dict[str, str]
) -> tuple[str | None, tuple[str, str] | None]:
    tier = None
    locator = None
    value = None
    extracted = None
    if step.action == "navigate":
        session.page.goto(session.base_url + step.path)
    elif step.action == "click":
        locator, tier = resolve(session.page, step.target)
        locator.click(timeout=step.timeout_ms)
    elif step.action == "type":
        locator, tier = resolve(session.page, step.target)
        value = bind_value(step.value, params, secrets)
        locator.fill(value, timeout=step.timeout_ms)
    elif step.action == "select":
        locator, tier = resolve(session.page, step.target)
        value = bind_value(step.value, params, secrets)
        locator.select_option(label=value, timeout=step.timeout_ms)
    elif step.action == "extract":
        assert_checkpoint(session.page, step.checkpoint, step.timeout_ms)
        locator, tier = resolve(session.page, step.target)
        text = locator.text_content() or ""
        match = re.search(step.parse.pattern, text)
        if match is None:
            raise ValueError(f"parse {step.parse.pattern!r} found nothing in {text!r}")
        return tier, (step.output, match.group(0))
    else:
        raise NotImplementedError(f"action not implemented yet: {step.action}")
    assert_checkpoint(session.page, step.checkpoint, step.timeout_ms, locator, value)
    return tier, extracted


def describe_checkpoint(checkpoint: Checkpoint) -> str:
    if isinstance(checkpoint, RoleVisible):
        return f'{checkpoint.role} "{checkpoint.name}" visible'
    if isinstance(checkpoint, ValueMatches):
        return "field holds the entered value"
    return f"{checkpoint.kind} {checkpoint.pattern!r}"


def observe_page(page: Page) -> str:
    """What the page actually shows, for the failure report."""
    try:
        url = re.sub(r";jsessionid=[^\s?#]+", "", page.url)
        headings = page.get_by_role("heading").all_inner_texts()
        return f"url: {url}; headings: {[h.strip() for h in headings if h.strip()]}"
    except Exception as exc:
        return f"page state unreadable: {type(exc).__name__}"


def classify_failure(
    page: Page, step: Step, exc: Exception, secrets: dict[str, str]
) -> BusinessOutcome | HardFailure:
    #known 'no' answers first: the step's declared detectors against the live page
    if step.on_fail:
        try:
            body = page.locator("body").inner_text()
        except Exception:
            body = ""
        for detector in step.on_fail:
            if re.search(detector.pattern, body):
                return BusinessOutcome(name=detector.outcome, step=step.id)
    #unknown: hard failure with evidence, never interpretation
    Path("evidence").mkdir(exist_ok=True)
    shot = f"evidence/replay_failure_{step.id}.png"
    try:
        page.screenshot(path=shot)
    except Exception:
        shot = None
    observed = f"{type(exc).__name__}: {str(exc).splitlines()[0]}; {observe_page(page)}"
    for secret_value in secrets.values():
        observed = observed.replace(secret_value, "[secret]")
    return HardFailure(
        step=step.id,
        expected=describe_checkpoint(step.checkpoint),
        observed=observed,
        screenshot=shot,
    )


def verify_step(session: Session, step: Step, params: dict, secrets: dict) -> None:
    """Assert a step's checkpoint, e.g. after a human performed the step manually."""
    locator = None
    value = None
    if step.target is not None:
        locator, _ = resolve(session.page, step.target)
    if step.value is not None:
        value = bind_value(step.value, params, secrets)
    assert_checkpoint(session.page, step.checkpoint, step.timeout_ms, locator, value)


def replay(
    artifact: Artifact,
    session: Session,
    params: dict[str, str] | None = None,
    secrets: dict[str, str] | None = None,
    operator: OperatorChannel | None = None,
    preapproved: bool = False,
    policy: Policy | None = None,
) -> ReplayResult:
    params = params or {}
    secrets = secrets or {}
    policy = policy or load_policy()
    check_origin(policy, session.base_url)
    results: list[StepResult] = []
    outputs: dict[str, str] = {}
    interventions: list[InterventionRecord] = []

    def failed(outcome) -> ReplayResult:
        return ReplayResult(ok=False, capability=artifact.name, outcome=outcome,
                            steps=results, outputs=outputs, interventions=interventions)

    #every capability starts at its declared entry point
    session.page.goto(session.base_url + artifact.surface.entry_path)
    for step in artifact.steps:
        check_action(policy, step.action)
        handling = policy.risk[step.risk]
        if handling == "block":
            return failed(HardFailure(
                step=step.id, expected="a step class the policy permits",
                observed=f"policy blocks '{step.risk}' steps outright"))
        #risky steps need a decision before anything touches the page
        if handling == "require_confirmation":
            if preapproved:
                interventions.append(InterventionRecord(
                    step=step.id, reason=f"risk: {step.risk}",
                    decision="approved", note="pre-approved (unattended)"))
            elif operator is None:
                return failed(HardFailure(
                    step=step.id, expected=f"approval for {step.risk} step",
                    observed="no operator channel and not pre-approved"))
            else:
                answer = operator.request(InterventionRequest(
                    capability=artifact.name, step=step.id,
                    reason=f"step is {step.risk}: a human must decide before it runs"))
                interventions.append(InterventionRecord(
                    step=step.id, reason=f"risk: {step.risk}",
                    decision=answer.decision, note=answer.note))
                if answer.decision == "abort":
                    return failed(HardFailure(
                        step=step.id, expected=f"approval for {step.risk} step",
                        observed="aborted by operator"))
                if answer.decision == "fixed":
                    #the human performed the step by hand; verify it like machine work
                    verify_step(session, step, params, secrets)
                    results.append(StepResult(
                        id=step.id, action=step.action, ok=True, ms=0, tier="human"))
                    continue

        started = time.monotonic()
        try:
            tier, extracted = run_step(session, step, params, secrets)
            if extracted is not None:
                outputs[extracted[0]] = extracted[1]
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            outcome = classify_failure(session.page, step, exc, secrets)
            if isinstance(outcome, HardFailure) and operator is not None:
                answer = operator.request(InterventionRequest(
                    capability=artifact.name, step=step.id, reason="step failed",
                    expected=outcome.expected, observed=outcome.observed,
                    screenshot=outcome.screenshot))
                interventions.append(InterventionRecord(
                    step=step.id, reason="hard_failure",
                    decision=answer.decision, note=answer.note))
                if answer.decision == "fixed":
                    try:
                        verify_step(session, step, params, secrets)
                        results.append(StepResult(
                            id=step.id, action=step.action, ok=True, ms=elapsed, tier="human"))
                        continue
                    except Exception as verify_exc:
                        outcome = classify_failure(session.page, step, verify_exc, secrets)
            results.append(StepResult(id=step.id, action=step.action, ok=False, ms=elapsed))
            return failed(outcome)
        elapsed = int((time.monotonic() - started) * 1000)
        results.append(
            StepResult(id=step.id, action=step.action, ok=True, ms=elapsed, tier=tier)
        )
    return ReplayResult(
        ok=True, capability=artifact.name, outcome=Success(),
        steps=results, outputs=outputs, interventions=interventions,
    )


def load_env(path: str = ".env") -> dict[str, str]:
    #.env values, falling back to the process environment
    env = dict(os.environ)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in os.environ:
                    continue  #real environment beats the file
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env[k] = v
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description="replay an artifact")
    parser.add_argument("artifact")
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--unattended", action="store_true",
                        help="no operator; risky steps run on the caller's pre-approval")
    parser.add_argument("--evidence", metavar="NAME",
                        help="also write the result JSON to evidence/NAME.json")
    args = parser.parse_args()

    artifact = load_artifact(args.artifact)
    params = dict(p.split("=", 1) for p in args.param)
    secrets = {
        k[len("SECRET_"):].lower(): v
        for k, v in load_env().items()
        if k.startswith("SECRET_")
    }
    operator = None if args.unattended else TerminalOperator()
    with Session(args.base_url, headless=not args.headed) as session:
        result = replay(artifact, session, params, secrets,
                        operator=operator, preapproved=args.unattended)
        if args.headed:
            input("press Enter to close the browser ")
    print(result.model_dump_json(indent=2))
    if args.evidence:
        Path("evidence").mkdir(exist_ok=True)
        Path(f"evidence/{args.evidence}.json").write_text(result.model_dump_json(indent=2))
        print(f"evidence saved to evidence/{args.evidence}.json")
    #exit codes: 0 success, 2 known business outcome, 1 hard failure
    codes = {"success": 0, "business": 2, "hard_failure": 1}
    raise SystemExit(codes[result.outcome.kind])


if __name__ == "__main__":
    main()
