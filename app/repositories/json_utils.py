import json
from typing import Any

from fastapi.encoders import jsonable_encoder


def dumps(value: Any) -> str:
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)
