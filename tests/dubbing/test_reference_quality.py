from __future__ import annotations

from src.dubbing.pipeline import _basic_reference_transcript_quality


def test_reference_quality_rejects_dangling_fragment() -> None:
    ok, reasons = _basic_reference_transcript_quality(
        "And that's.",
        {"quality_min_words": 2, "quality_max_words": 30, "quality_require_sentence_end": True},
    )
    assert not ok
    assert "DANGLING_END" in reasons


def test_reference_quality_accepts_complete_sentence() -> None:
    ok, reasons = _basic_reference_transcript_quality(
        "I think investors are overlooking one important part of the AI trade.",
        {"quality_min_words": 8, "quality_max_words": 34, "quality_require_sentence_end": True},
    )
    assert ok
    assert reasons == []


def test_auxiliary_ai_helper_requires_explicit_paid_api_permission(tmp_path) -> None:
    from src.dubbing.pipeline import DubbingError, DubbingPipeline

    pipeline = DubbingPipeline(
        tmp_path,
        {},
        python_executable=tmp_path / "python.exe",
        runtime_preflight=lambda *_args, **_kwargs: {"ready": True},
        allow_paid_api=False,
    )
    try:
        pipeline._run_stage3_json_helper(
            "src.stage3.reference_quality",
            request_path=tmp_path / "request.json",
            response_path=tmp_path / "response.json",
        )
    except DubbingError as exc:
        assert exc.code == "PAID_API_NOT_ALLOWED"
    else:
        raise AssertionError("auxiliary AI helper should require explicit paid API permission")
