"""Graph assembly.
"""

from __future__ import annotations
from functools import partial
from pathlib import Path
from typing import Any
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from ..domain.enums import StageName
from ..logging import get_logger
from .context import RunContext
from .nodes.angles import synthesize_angles_node
from .nodes.images import master_scene_node, render_square_node, render_vertical_node
from .nodes.selection import await_selection_node
from .nodes.spec import build_spec_node
from .nodes.validate import validate_brief_node
from .nodes.video import render_video_node
from .state import CampaignState
from .nodes.research import research_node 

log = get_logger(__name__)

_NODES = {
    StageName.VALIDATE_BRIEF: validate_brief_node,
    StageName.RESEARCH: None, 
    StageName.SYNTHESIZE_ANGLES: synthesize_angles_node,
    StageName.AWAIT_SELECTION: await_selection_node,
    StageName.BUILD_SPEC: build_spec_node,
    StageName.MASTER_SCENE: master_scene_node,
    StageName.RENDER_SQUARE: render_square_node,
    StageName.RENDER_VERTICAL: render_vertical_node,
    StageName.RENDER_VIDEO: render_video_node,
}


def build_graph(ctx: RunContext, checkpointer: Any) -> Any:
    """Compile the campaign workflow for one run context."""

    nodes = dict(_NODES)
    nodes[StageName.RESEARCH] = research_node

    graph: StateGraph = StateGraph(CampaignState)
    for stage, fn in nodes.items():
        graph.add_node(stage.value, partial(_invoke, fn, ctx))

    graph.add_edge(START, StageName.VALIDATE_BRIEF.value)
    graph.add_edge(StageName.VALIDATE_BRIEF.value, StageName.RESEARCH.value)
    graph.add_edge(StageName.RESEARCH.value, StageName.SYNTHESIZE_ANGLES.value)
    graph.add_edge(StageName.SYNTHESIZE_ANGLES.value, StageName.AWAIT_SELECTION.value)
    graph.add_edge(StageName.AWAIT_SELECTION.value, StageName.BUILD_SPEC.value)
    graph.add_edge(StageName.BUILD_SPEC.value, StageName.MASTER_SCENE.value)

    # Fan out: both formats depend only on the master scene.
    graph.add_edge(StageName.MASTER_SCENE.value, StageName.RENDER_SQUARE.value)
    graph.add_edge(StageName.MASTER_SCENE.value, StageName.RENDER_VERTICAL.value)

    # Join: the video needs the finished vertical asset.
    graph.add_edge(StageName.RENDER_VERTICAL.value, StageName.RENDER_VIDEO.value)

    graph.add_edge(StageName.RENDER_SQUARE.value, END)
    graph.add_edge(StageName.RENDER_VIDEO.value, END)

    return graph.compile(checkpointer=checkpointer)


async def _invoke(fn: Any, ctx: RunContext, state: CampaignState) -> dict[str, Any]:
    return await fn(state, ctx)


def checkpointer_context(path: Path) -> Any:
    """Async SQLite checkpointer, used as an async context manager.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(path))


def concurrency_summary() -> dict[str, Any]:
    """Machine-readable description of the graph's parallelism, for the UI."""
    return {
        "sequential": [
            StageName.VALIDATE_BRIEF.value,
            StageName.RESEARCH.value,
            StageName.SYNTHESIZE_ANGLES.value,
            StageName.AWAIT_SELECTION.value,
            StageName.BUILD_SPEC.value,
            StageName.MASTER_SCENE.value,
        ],
        "concurrent_groups": [[StageName.RENDER_SQUARE.value, StageName.RENDER_VERTICAL.value]],
        "joins": {
            StageName.RENDER_VIDEO.value: [StageName.RENDER_VERTICAL.value],
        },
        "rationale": (
            "The square and vertical renders depend only on the master scene and "
            "not on each other, so they run in the same superstep. The video is "
            "built from the finished 1080x1920 asset and therefore waits. Research "
            "is sequential because each search depends on the previous result."
        ),
    }
