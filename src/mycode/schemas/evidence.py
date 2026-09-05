from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class UrlEvidence:
    raw_url: str
    normalized_url: str
    domain: str
    url_type: str
    role: str
    leakage_risk: str
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ImageEvidence:
    raw_url: str
    source_field: str
    image_type: str
    role: str
    extension: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceSketch:
    instance_id: str
    repo: str
    dataset: str
    issue_text: str
    urls: List[UrlEvidence] = field(default_factory=list)
    images: List[ImageEvidence] = field(default_factory=list)
    concern_queries: List[str] = field(default_factory=list)
    symbol_queries: List[str] = field(default_factory=list)
    flow_hypotheses: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["urls"] = [u.to_dict() for u in self.urls]
        data["images"] = [i.to_dict() for i in self.images]
        return data


@dataclass
class EvidencePacket:
    instance_id: str
    repo: str
    dataset: str
    issue_summary: str
    modality: str
    url_inspections: List[Dict[str, Any]] = field(default_factory=list)
    reproduction_cases: List[Dict[str, Any]] = field(default_factory=list)
    web_snapshots: List[Dict[str, Any]] = field(default_factory=list)
    image_inspections: List[Dict[str, Any]] = field(default_factory=list)
    code_references: List[Dict[str, Any]] = field(default_factory=list)
    symbol_queries: List[str] = field(default_factory=list)
    concern_queries: List[str] = field(default_factory=list)
    flow_hypotheses: List[str] = field(default_factory=list)
    search_plan: List[Dict[str, Any]] = field(default_factory=list)
    leakage: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolRequest:
    tool: str
    source: str
    evidence_type: str
    role_hypothesis: str
    priority: str
    reason: str
    expected_outputs: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    should_execute: bool = True
    caution: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolObservation:
    tool: str
    source: str
    success: bool
    status: str
    extracted: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    cache_path: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvidenceCollectionPlan:
    instance_id: str
    repo: str
    dataset: str
    issue_summary: str
    suspected_problem_types: List[str] = field(default_factory=list)
    tool_requests: List[ToolRequest] = field(default_factory=list)
    initial_search_intent: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    llm_planning: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tool_requests"] = [request.to_dict() for request in self.tool_requests]
        return data


@dataclass(frozen=True)
class NormalizedSample:
    instance_id: str
    repo: str
    dataset: str
    issue_text: str
    raw: Dict[str, Any]
    gold_files: List[str] = field(default_factory=list)
    language: Optional[str] = None
