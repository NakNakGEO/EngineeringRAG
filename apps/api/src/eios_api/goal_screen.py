"""Screening of free-text goals for live external-database intent (advisory, widen-only).

``/team/select`` and ``POST /workflows`` take a goal in plain words. If it reads as wanting a live
connection to an external, company, QA or production database, it is never silently accepted: the
risk is forced to ``critical`` (so every human gate applies), a policy denial is audited and put on
the event stream, and the response carries the reason. The structural defences (no drivers, no
database ports or clients, network-isolated sandbox) hold regardless of this heuristic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from eios_domain.events import ActorType
from eios_domain.policy import PolicyRequest, Risk
from eios_domain.registry import EXTERNAL_DATABASE_REASON, find_external_database_intent
from eios_observability.recorder import RunContext
from eios_runtime import Container

# "database" in the action name routes the request to the Root Policy's external-database rule.
SCREEN_ACTION = "goal.external_database_request"


@dataclass(frozen=True)
class GoalScreen:
    requested_risk: Risk
    matched: str | None = None

    @property
    def blocked(self) -> bool:
        return self.matched is not None

    @property
    def risk(self) -> Risk:
        return Risk.CRITICAL if self.blocked else self.requested_risk

    def notice(self, run_id: uuid.UUID | None = None) -> dict[str, Any]:
        """The reason, for the response body. Empty when the goal is fine."""
        if self.matched is None:
            return {}
        return {
            "policy_notice": {
                "blocked": True,
                "rule": "root.external_database",
                "reason": (
                    f"{EXTERNAL_DATABASE_REASON} The goal reads as wanting live database "
                    f"access (matched: '{self.matched}'), so it runs at critical risk with "
                    "every human gate in force. If no live access is intended, rephrase it "
                    "as text work, for example 'generate SQL as text for a human to run'."
                ),
                "matched": self.matched,
                "requested_risk": self.requested_risk.value,
                "enforced_risk": Risk.CRITICAL.value,
                "human_gate": True,
                "run_id": str(run_id) if run_id else None,
            }
        }

    async def record(
        self, container: Container, goal: str, run_id: uuid.UUID | None = None
    ) -> dict[str, Any]:
        """Audit the denial and emit its event, then return the response fields."""
        if self.matched is None:
            return {}
        if run_id is None:
            async with container.recorder.run(kind="goal_screening", goal=goal[:200]) as ctx:
                await self._deny(container, goal, ctx)
            return self.notice(ctx.run_id)
        await self._deny(container, goal, await container.recorder.resume(run_id))
        return self.notice(run_id)

    async def _deny(self, container: Container, goal: str, ctx: RunContext) -> None:
        await container.policy.evaluate(
            PolicyRequest(
                actor_type=ActorType.SYSTEM,
                actor_id="goal_screen",
                action=SCREEN_ACTION,
                target=goal[:200],
                run_id=ctx.run_id,
                attributes={
                    "matched": self.matched,
                    "requested_risk": self.requested_risk.value,
                },
            ),
            ctx,
        )


def screen_goal(goal: str, risk: Risk) -> GoalScreen:
    return GoalScreen(requested_risk=risk, matched=find_external_database_intent(goal))
