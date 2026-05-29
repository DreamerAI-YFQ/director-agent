"""
Pipeline output schema validation.

The prompt manifest defines the expected output fields and element_id prefix
for each P0-P5 stage. This module validates provider results without failing
the pipeline run, so bad or weak outputs are visible in persisted stage data.
"""

from typing import Any

from agent.prompt_manager import get_prompt_manager
from agent.reference_tracker import extract_elements


STAGE_FALLBACKS = {
    "P0": {
        "element_id_prefix": "META",
        "fields": [
            "video_id",
            "duration",
            "platform",
            "language",
            "content_type",
            "product_brand",
            "product_name",
            "product_category",
            "tags",
        ],
    },
    "P1": {
        "element_id_prefix": "VIS",
        "fields": [
            "scenes",
            "shot_composition",
            "color_palette",
            "on_screen_text",
            "people",
            "product_presentation",
        ],
    },
    "P2": {
        "element_id_prefix": "AUD",
        "fields": [
            "transcription",
            "script_structure",
            "rhetoric_techniques",
            "health_claims",
            "audio_features",
            "cta",
        ],
    },
    "P3": {
        "element_id_prefix": "STR",
        "fields": [
            "hook_analysis",
            "narrative_arc",
            "emotion_curve",
            "rhythm",
            "structure_template",
            "novel_tags",
        ],
    },
    "P4": {
        "element_id_prefix": "COMP",
        "fields": [
            "compliance_risks",
            "cta_compliance",
            "brand_fit",
            "performance_prediction",
            "cross_model_validation",
        ],
    },
    "P5": {
        "element_id_prefix": "RPT",
        "fields": [
            "fusion_decisions",
            "full_report",
            "rag_index",
            "novel_items",
            "dictionary_snapshot",
            "quality_check",
        ],
    },
}


META_KEYS = {
    "_token_stats",
    "_schema_validation",
    "cross_validation",
    "parse_warning",
    "raw_text",
}


def _stage_name(stage: Any) -> str:
    return getattr(stage, "value", stage)


def _manifest_schema(stage: str, prompt_manager=None) -> dict:
    pm = prompt_manager or get_prompt_manager()
    prompts = getattr(pm, "_manifest", {}).get("prompts", {})
    prompt_name = ""
    schema = {}

    for name, info in prompts.items():
        if name.startswith(f"pipeline_{stage}_"):
            prompt_name = name
            schema = info.get("output_schema", {}) or {}
            break

    fallback = STAGE_FALLBACKS.get(stage, {})
    return {
        "prompt_name": prompt_name,
        "fields": list(schema.get("fields") or fallback.get("fields") or []),
        "element_id_prefix": schema.get("element_id_prefix")
        or fallback.get("element_id_prefix", ""),
    }


def _collect_field_markers(obj: Any) -> set[str]:
    markers: set[str] = set()

    def visit(value: Any):
        if isinstance(value, dict):
            field_name = value.get("field")
            if isinstance(field_name, str) and field_name:
                markers.add(field_name)

            for key, nested in value.items():
                if key in META_KEYS:
                    continue
                markers.add(str(key))
                visit(nested)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(obj)
    return markers


def validate_stage_output(stage: Any, result: dict, prompt_manager=None) -> dict:
    """
    Validate a provider result against the manifest output schema.

    The returned dict is JSON serializable and intentionally descriptive. The
    pipeline stores it under result["_schema_validation"] for later inspection.
    """
    stage_name = _stage_name(stage)
    schema = _manifest_schema(stage_name, prompt_manager)
    expected_fields = schema["fields"]
    expected_prefix = schema["element_id_prefix"]

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(result, dict):
        return {
            "valid": False,
            "stage": stage_name,
            "prompt_name": schema["prompt_name"],
            "element_id_prefix": expected_prefix,
            "extracted_elements": [],
            "missing_fields": expected_fields,
            "errors": ["result is not an object"],
            "warnings": [],
        }

    output = result.get("output")
    if not isinstance(output, dict):
        errors.append("missing or non-object output")
        output = {}

    parse_warning = result.get("parse_warning") or output.get("parse_warning")
    if parse_warning:
        errors.append("output contains parse_warning")
    if "raw_text" in output:
        errors.append("output contains raw_text instead of structured data")

    declared_stage = result.get("pipeline_stage")
    if declared_stage and str(declared_stage).replace("_cv", "") != stage_name:
        warnings.append(f"pipeline_stage is {declared_stage}, expected {stage_name}")

    declared_prefix = result.get("element_id_prefix")
    if declared_prefix and expected_prefix and declared_prefix != expected_prefix:
        errors.append(f"element_id_prefix is {declared_prefix}, expected {expected_prefix}")
    elif expected_prefix and not declared_prefix:
        warnings.append("element_id_prefix is not declared in result")

    elements = extract_elements(output)
    element_ids = [e["id"] for e in elements if e.get("id")]
    if expected_prefix:
        expected_head = f"{expected_prefix}_"
        if not any(eid.startswith(expected_head) for eid in element_ids):
            errors.append(f"no element_id with prefix {expected_head} found")

    field_markers = _collect_field_markers(output)
    missing_fields = [field for field in expected_fields if field not in field_markers]
    if missing_fields:
        errors.append(f"missing required fields: {', '.join(missing_fields)}")

    if not output:
        errors.append("output is empty")

    return {
        "valid": len(errors) == 0,
        "stage": stage_name,
        "prompt_name": schema["prompt_name"],
        "element_id_prefix": expected_prefix,
        "extracted_elements": elements,
        "missing_fields": missing_fields,
        "errors": errors,
        "warnings": warnings,
    }
