"""Target resolution: walk the descriptor tiers, fail on ambiguity."""

from __future__ import annotations

from playwright.sync_api import Locator, Page

from app.schema import TargetDescriptor


class TargetNotFound(Exception):
    pass


class AmbiguousTarget(Exception):
    pass


def resolve(page: Page, target: TargetDescriptor) -> tuple[Locator, str]:
    """Return (locator, tier). Fall through on zero matches, raise on more than one."""
    if target.primary is not None:
        locator = page.get_by_role(target.primary.role, name=target.primary.name, exact=True)
        count = locator.count()
        if count == 1:
            return locator, "primary"
        if count > 1:
            raise AmbiguousTarget(
                f"primary {target.primary.role} {target.primary.name!r} matched {count} elements"
            )
    if target.fallback is not None:
        locator = page.locator(target.fallback.css)
        count = locator.count()
        if count == 1:
            return locator, "fallback"
        if count > 1:
            raise AmbiguousTarget(f"fallback {target.fallback.css!r} matched {count} elements")
    if target.last is not None:
        raise NotImplementedError("last tier not implemented yet")
    raise TargetNotFound("no tier matched an element")
