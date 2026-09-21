"""One explicit, fail-closed experiment arm per process."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from functools import wraps


ARMS = ("full", "no_visual", "no_graph", "no_flow", "no_graph_flow",
        "no_closure", "fixed_react", "no_head", "no_checkpoint")


@dataclass(frozen=True)
class AblationConfig:
    arm: str = "full"

    def __post_init__(self):
        if self.arm not in ARMS:
            raise ValueError(f"Unknown MAGNET_ABLATION={self.arm!r}; expected {ARMS}")

    @property
    def visual(self):
        return self.arm != "no_visual"

    @property
    def graph(self):
        return self.arm not in {"no_graph", "no_graph_flow"}

    @property
    def flow(self):
        return self.arm not in {"no_flow", "no_graph_flow"}

    def tools(self):
        return [tool for tool in ("SearchAnchor", "NavigateCode", "TraceFlow", "ReadCode")
                if (tool != "NavigateCode" or self.graph)
                and (tool != "TraceFlow" or self.flow)]

    def to_dict(self):
        return {**asdict(self), "visual": self.visual, "graph_navigation": self.graph,
                "flow_evidence": self.flow, "tools": self.tools(),
                "modification_closure": self.arm != "no_closure",
                "fixed_react_only": self.arm == "fixed_react"}


def ablation() -> AblationConfig:
    return AblationConfig(os.environ.get("MAGNET_ABLATION", "full"))


def flow_backend(fn):
    """Guard all flow entry points, including lightweight and nested callers."""
    @wraps(fn)
    def guarded(*args, **kwargs):
        if not ablation().flow:
            return []
        return fn(*args, **kwargs)
    return guarded
