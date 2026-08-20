"""OAC legacy source projection for Core's process-owned Registry Snapshot."""

from app.application import RegistrySnapshotSourceInput, RegistrySnapshotSourceState
from host_adapters.oac.mappers.registry import registry_definitions_to_snapshot_inputs


def map_oac_legacy_registry_snapshot(
    state: RegistrySnapshotSourceState,
) -> RegistrySnapshotSourceInput:
    """Return safe v2 candidate input; Core owns the live Snapshot write."""

    return RegistrySnapshotSourceInput(
        source="oac_legacy_registry",
        definitions=registry_definitions_to_snapshot_inputs(state.agents),
    )
