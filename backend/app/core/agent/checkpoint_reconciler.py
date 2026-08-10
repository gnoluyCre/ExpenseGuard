"""Mirror authoritative F7 business facts into a LangGraph checkpoint."""

from __future__ import annotations

import uuid
from typing import TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.core.agent.service_models import ReconciliationSnapshot


class InvestigationCheckpointState(TypedDict):
    investigation_run_id: str
    committed_step_count: int
    step_fingerprints: list[str]
    terminal: bool


def _accept_authoritative_snapshot(
    state: InvestigationCheckpointState,
) -> InvestigationCheckpointState:
    if state["committed_step_count"] != len(state["step_fingerprints"]):
        raise ValueError("checkpoint step count does not match fingerprints")
    return state


class LangGraphCheckpointReconciler:
    """A checkpoint cache that is always overwritten from public-schema facts."""

    def __init__(self, checkpointer: BaseCheckpointSaver[str]) -> None:
        builder: StateGraph[
            InvestigationCheckpointState,
            None,
            InvestigationCheckpointState,
            InvestigationCheckpointState,
        ] = StateGraph(InvestigationCheckpointState)
        builder.add_node("accept_business_facts", _accept_authoritative_snapshot)
        builder.add_edge(START, "accept_business_facts")
        builder.add_edge("accept_business_facts", END)
        self._graph: CompiledStateGraph[
            InvestigationCheckpointState,
            None,
            InvestigationCheckpointState,
            InvestigationCheckpointState,
        ] = builder.compile(checkpointer=checkpointer, name="f7-investigation-reconciler")

    async def reconcile(self, snapshot: ReconciliationSnapshot) -> None:
        state = InvestigationCheckpointState(
            investigation_run_id=str(snapshot.investigation_run_id),
            committed_step_count=len(snapshot.committed_steps),
            step_fingerprints=[step.payload_fingerprint for step in snapshot.committed_steps],
            terminal=snapshot.terminal,
        )
        await self._graph.ainvoke(state, config=_config(snapshot.investigation_run_id))

    async def read(self, investigation_run_id: uuid.UUID) -> InvestigationCheckpointState | None:
        snapshot = await self._graph.aget_state(_config(investigation_run_id))
        if not snapshot.values:
            return None
        return cast(InvestigationCheckpointState, dict(snapshot.values))


def _config(investigation_run_id: uuid.UUID) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": str(investigation_run_id),
        }
    }
