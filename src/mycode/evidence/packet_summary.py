from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable

from mycode.schemas.evidence import EvidencePacket


def summarize_packets(packets: Iterable[EvidencePacket]) -> Dict[str, Any]:
    packet_list = list(packets)
    modality = Counter(packet.modality for packet in packet_list)
    url_roles = Counter()
    url_tools = Counter()
    reproduction_platforms = Counter()
    image_types = Counter()
    search_stages = Counter()
    leakage_count = 0

    for packet in packet_list:
        leakage_count += len(packet.leakage)
        for item in packet.url_inspections:
            url_roles[item.get("role", "unknown")] += 1
            url_tools[item.get("tool_recommendation", "unknown")] += 1
        for case in packet.reproduction_cases:
            reproduction_platforms[case.get("platform", "unknown")] += 1
        for image in packet.image_inspections:
            image_types[image.get("image_type", "unknown")] += 1
        for step in packet.search_plan:
            search_stages[step.get("stage", "unknown")] += 1

    return {
        "samples": len(packet_list),
        "modality": dict(sorted(modality.items())),
        "url_roles": dict(sorted(url_roles.items())),
        "url_tools": dict(sorted(url_tools.items())),
        "reproduction_platforms": dict(sorted(reproduction_platforms.items())),
        "image_types": dict(sorted(image_types.items())),
        "search_stages": dict(sorted(search_stages.items())),
        "leakage_urls": leakage_count,
    }

