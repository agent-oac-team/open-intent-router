from argparse import Namespace

import pytest

from scripts.initialize_milvus_domains import initialize


def test_initialize_rejects_shared_knowledge_and_memory_file(tmp_path) -> None:
    shared = tmp_path / "shared.db"
    args = Namespace(
        knowledge_uri=str(shared),
        memory_uri=str(shared),
        knowledge_collection="knowledge",
        memory_collection="memory",
        rehearsal_knowledge_collection="knowledge_rehearsal",
        rehearsal_memory_collection="memory_rehearsal",
        dimension=1024,
    )

    with pytest.raises(ValueError, match="must be distinct"):
        initialize(args)
