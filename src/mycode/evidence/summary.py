from __future__ import annotations

from collections import Counter
from typing import Iterable

from mycode.schemas.evidence import EvidenceSketch


def summarize_evidence(sketches: Iterable[EvidenceSketch]) -> dict:
    sketches = list(sketches)
    url_types = Counter()
    url_roles = Counter()
    url_risks = Counter()
    image_types = Counter()
    modality = Counter()
    flow_hypotheses = Counter()

    for sketch in sketches:
        has_url = bool(sketch.urls)
        has_image = bool(sketch.images)
        if has_url and has_image:
            modality["image_and_url"] += 1
        elif has_url:
            modality["url_only"] += 1
        elif has_image:
            modality["image_only"] += 1
        else:
            modality["text_only"] += 1

        for url in sketch.urls:
            url_types[url.url_type] += 1
            url_roles[url.role] += 1
            url_risks[url.leakage_risk] += 1
        for image in sketch.images:
            image_types[image.image_type] += 1
        for hyp in sketch.flow_hypotheses:
            flow_hypotheses[hyp] += 1

    return {
        "samples": len(sketches),
        "modality": dict(modality),
        "url_types": dict(url_types),
        "url_roles": dict(url_roles),
        "url_leakage_risk": dict(url_risks),
        "image_types": dict(image_types),
        "flow_hypotheses": dict(flow_hypotheses),
    }

