from agent.pipeline_schema import validate_stage_output


def keyed_output(prefix, fields):
    return {
        f"{prefix}_{idx:03d}": {
            "field": field,
            "value": f"value_for_{field}",
        }
        for idx, field in enumerate(fields, start=1)
    }


def test_valid_p5_keyed_output_passes_schema_validation():
    fields = [
        "fusion_decisions",
        "full_report",
        "rag_index",
        "novel_items",
        "dictionary_snapshot",
        "quality_check",
    ]
    result = {
        "pipeline_stage": "P5",
        "element_id_prefix": "RPT",
        "output": keyed_output("RPT", fields),
    }

    validation = validate_stage_output("P5", result)

    assert validation["valid"] is True
    assert validation["missing_fields"] == []
    assert [e["id"] for e in validation["extracted_elements"]] == [
        "RPT_001",
        "RPT_002",
        "RPT_003",
        "RPT_004",
        "RPT_005",
        "RPT_006",
    ]


def test_raw_text_parse_warning_output_fails_schema_validation():
    result = {
        "pipeline_stage": "P5",
        "output": {
            "raw_text": "not json",
            "parse_warning": "未能解析为结构化JSON",
        },
    }

    validation = validate_stage_output("P5", result)

    assert validation["valid"] is False
    assert "output contains parse_warning" in validation["errors"]
    assert "output contains raw_text instead of structured data" in validation["errors"]


def test_missing_fields_are_reported():
    result = {
        "pipeline_stage": "P0",
        "element_id_prefix": "META",
        "output": {
            "META_001": {
                "field": "video_id",
                "value": "v1",
            }
        },
    }

    validation = validate_stage_output("P0", result)

    assert validation["valid"] is False
    assert "platform" in validation["missing_fields"]
    assert "duration" in validation["missing_fields"]
