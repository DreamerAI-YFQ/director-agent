from agent.reference_tracker import extract_and_register_references, extract_elements


def test_extract_elements_supports_key_as_element_id():
    output = {
        "META_001": {
            "field": "video_id",
            "value": "vid_001",
        }
    }

    elements = extract_elements(output)

    assert elements == [
        {
            "id": "META_001",
            "type": "meta",
            "name": "video_id",
        }
    ]


def test_extract_elements_keeps_nested_id_support():
    output = {
        "segments": [
            {
                "id": "STR_001",
                "name": "problem-agitation-solution",
            }
        ]
    }

    elements = extract_elements(output)

    assert elements == [
        {
            "id": "STR_001",
            "type": "segments",
            "name": "problem-agitation-solution",
        }
    ]


def test_extract_and_register_references_uses_full_pipeline_map(monkeypatch):
    calls = []

    def fake_register_reference(**kwargs):
        calls.append(kwargs)
        return {"ref_id": len(calls)}

    monkeypatch.setattr(
        "agent.reference_tracker.register_reference",
        fake_register_reference,
    )

    stage_outputs = {
        "P0": {"output": {"META_001": {"field": "video_id", "value": "v1"}}},
        "P1": {"output": {"VIS_001": {"field": "scenes", "value": []}}},
        "P2": {"output": {}},
        "P3": {"output": {}},
        "P4": {"output": {}},
        "P5": {"output": {}},
    }

    result = extract_and_register_references("run_test", stage_outputs)

    assert result == {"registered": 8, "errors": []}
    assert {
        "source_stage": "P0",
        "source_element_id": "META_001",
        "target_stage": "P1",
        "target_pipeline_run_id": "run_test",
        "reference_type": "direct",
    } in calls
    assert {
        "source_stage": "P0",
        "source_element_id": "META_001",
        "target_stage": "P5",
        "target_pipeline_run_id": "run_test",
        "reference_type": "direct",
    } in calls
    assert {
        "source_stage": "P1",
        "source_element_id": "VIS_001",
        "target_stage": "P3",
        "target_pipeline_run_id": "run_test",
        "reference_type": "direct",
    } in calls
