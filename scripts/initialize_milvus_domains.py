#!/usr/bin/env python3
"""Create isolated primary and rehearsal Milvus Lite collections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def ensure_collection(
    client,
    *,
    collection_name: str,
    dimension: int,
    primary_field_name: str,
) -> str:
    if client.has_collection(collection_name):
        return "existing"
    client.create_collection(
        collection_name=collection_name,
        dimension=dimension,
        primary_field_name=primary_field_name,
        id_type="string",
        vector_field_name="vector",
        metric_type="COSINE",
        auto_id=False,
    )
    return "created"


def initialize(args: argparse.Namespace) -> dict:
    knowledge_uri = Path(args.knowledge_uri)
    memory_uri = Path(args.memory_uri)
    if knowledge_uri.resolve() == memory_uri.resolve():
        raise ValueError("Knowledge and Memory Milvus files must be distinct")

    try:
        from pymilvus import MilvusClient
    except Exception as exc:
        raise RuntimeError("pymilvus and milvus-lite are required") from exc

    knowledge_uri.parent.mkdir(parents=True, exist_ok=True)
    memory_uri.parent.mkdir(parents=True, exist_ok=True)

    knowledge = MilvusClient(uri=str(knowledge_uri))
    memory = MilvusClient(uri=str(memory_uri))
    collections = {
        args.knowledge_collection: ensure_collection(
            knowledge,
            collection_name=args.knowledge_collection,
            dimension=args.dimension,
            primary_field_name="chunk_id",
        ),
        args.rehearsal_knowledge_collection: ensure_collection(
            knowledge,
            collection_name=args.rehearsal_knowledge_collection,
            dimension=args.dimension,
            primary_field_name="chunk_id",
        ),
        args.memory_collection: ensure_collection(
            memory,
            collection_name=args.memory_collection,
            dimension=args.dimension,
            primary_field_name="id",
        ),
        args.rehearsal_memory_collection: ensure_collection(
            memory,
            collection_name=args.rehearsal_memory_collection,
            dimension=args.dimension,
            primary_field_name="id",
        ),
    }
    return {
        "contract": "oir-milvus-domain-initialization/v1",
        "passed": True,
        "knowledge_file": str(knowledge_uri),
        "memory_file": str(memory_uri),
        "files_are_distinct": True,
        "dimension": args.dimension,
        "collections": collections,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-uri", default=".data/oir_knowledge_milvus.db")
    parser.add_argument("--memory-uri", default=".data/oir_memory_milvus.db")
    parser.add_argument("--knowledge-collection", default="oir_knowledge_vectors")
    parser.add_argument("--memory-collection", default="oir_memory_vectors")
    parser.add_argument(
        "--rehearsal-knowledge-collection", default="oir_knowledge_vectors_rehearsal"
    )
    parser.add_argument("--rehearsal-memory-collection", default="oir_memory_vectors_rehearsal")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = initialize(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
