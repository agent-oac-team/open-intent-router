from scripts.run_oac_fallback_drill import run_drill


async def test_fallback_drill_covers_all_required_safety_paths() -> None:
    report = await run_drill()

    assert report["passed"] is True
    assert report["circuit_states"] == ["closed", "open", "half_open", "closed"]
    assert report["fallback_calls"] == ["read", "route"]
