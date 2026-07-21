from scripts.validate_oac_entitlement_replay_dataset import validate


def test_oac_entitlement_replay_dataset_covers_blocking_matrix() -> None:
    payload = validate()
    assert len(payload["cases"]) == 8
