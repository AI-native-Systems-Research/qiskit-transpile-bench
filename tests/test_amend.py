"""Archived amendment metadata remains readable."""

from qtb.coordinator.amend import effective


def test_effective_applies_requirements_everywhere_and_hashes_only_to_their_stage():
    run = {
        "hashes": {"coordinator": "a", "implementation": "b", "harness": "w"},
        "cost_evidence": {"identity": "old"},
        "amendments": [
            {
                "stage": "cost",
                "hashes": {"coordinator": "c", "implementation": "d"},
                "session": {"cost_evidence": {"identity": "new"}},
            }
        ],
    }
    cost = effective(run, "cost")
    assert cost["hashes"] == {"coordinator": "c", "implementation": "d", "harness": "w"}
    assert cost["cost_evidence"] == {"identity": "new"}
    for stage in (None, "quality"):
        view = effective(run, stage)
        assert view["hashes"]["coordinator"] == "a" and view["cost_evidence"]["identity"] == "new"
    assert run["hashes"]["coordinator"] == "a"  # the record itself is untouched
    assert effective({"hashes": {}}) == {"hashes": {}}
