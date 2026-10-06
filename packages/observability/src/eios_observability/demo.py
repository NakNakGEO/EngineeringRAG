"""A demo operation that exercises the whole event vocabulary without agents or tools yet."""

from __future__ import annotations

import asyncio

from eios_domain.events import ActorType, EventStatus, EventType
from eios_observability.recorder import RunContext, RunRecorder


async def run_demo(
    recorder: RunRecorder,
    ctx: RunContext,
    *,
    fail: bool = False,
    step_delay: float = 0.0,
) -> None:
    """Emit a realistic event sequence under an already-started run, then complete or fail it."""

    async def pause() -> None:
        if step_delay:
            await asyncio.sleep(step_delay)

    try:
        await ctx.emit(EventType.GOAL_CLASSIFIED, "classified as demo", data={"task_type": "demo"})
        await pause()
        async with ctx.span(actor_type=ActorType.SYSTEM, actor_id="context-governor") as span:
            await span.emit(
                EventType.CONTEXT_REQUESTED, "context level L1", status=EventStatus.STARTED
            )
            await span.emit(
                EventType.RETRIEVAL_STARTED, "retrieval started", status=EventStatus.STARTED
            )
            await pause()
            for n in (1, 2):
                await span.emit(EventType.KNOWLEDGE_HIT, f"knowledge hit {n}", data={"rank": n})
            await span.emit(EventType.CONTEXT_EXPANDED, "expanded to L2", data={"level": "L2"})
        await pause()
        async with ctx.span(actor_type=ActorType.AGENT, actor_id="software-engineer") as agent:
            await ctx.charge("agent_calls")
            await agent.emit(EventType.AGENT_SELECTED, "primary agent selected")
            await agent.emit(EventType.CAPABILITY_REQUESTED, "capability: demo_echo")
            await agent.emit(
                EventType.POLICY_ALLOWED, "policy allowed demo_echo", actor_type=ActorType.SYSTEM
            )
            async with agent.span(actor_type=ActorType.TOOL, actor_id="demo-tool") as tool:
                await ctx.charge("tool_calls")
                await tool.emit(
                    EventType.TOOL_STARTED, "demo tool started", status=EventStatus.STARTED
                )
                await pause()
                await tool.emit(EventType.TOOL_COMPLETED, "demo tool completed")
            await ctx.charge("llm_calls")
            await ctx.charge("tokens", 1200)
            await agent.emit(
                EventType.LLM_STARTED,
                "llm call",
                status=EventStatus.STARTED,
                actor_type=ActorType.LLM,
            )
            await agent.emit(
                EventType.LLM_COMPLETED,
                "llm call finished",
                actor_type=ActorType.LLM,
                data={"tokens": 1200},
            )
            await agent.emit(EventType.EVIDENCE_CREATED, "demo evidence recorded")
        await pause()
        if fail:
            raise RuntimeError("demo failure requested")
    except Exception as exc:
        await recorder.fail_run(ctx, f"{type(exc).__name__}: {exc}")
        return
    await recorder.complete_run(ctx, "demo completed")
