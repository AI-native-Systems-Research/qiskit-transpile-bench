from qtb.config import load_profile
from qtb.coordinator import quality_cases
from qtb.coordinator.quality import RUNTIME_PROTOCOL


def test_quality_runtime_covers_full_current_profiles_without_small_cost_panels():
    for profile in ("iterations-profile", "confirm-profile"):
        manifest, policy, _ = load_profile(profile)
        assert manifest["version"] == policy["version"] == (
            14 if profile == "confirm-profile" else 11
        )
        assert quality_cases(manifest) == manifest["cases"]
        assert not {"timing", "memory"} & {case["role"] for case in manifest["cases"]}
        assert "quality/runtime" in policy["required_ids"]
        if profile == "confirm-profile":
            assert {case["optimization_level"] for case in manifest["cases"]} == {2, 3}
    assert RUNTIME_PROTOCOL == {
        "mode": "timing_e2e", "warmups": 1, "warmups_once": True,
        "minimum_calls": 1, "minimum_ns": 0,
    }
