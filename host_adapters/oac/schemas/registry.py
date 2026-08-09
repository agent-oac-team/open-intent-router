from pydantic import Field

from app.schemas.common import StrictBaseModel


class RegistryAgent(StrictBaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    bot_id: str = ""
    route_path: str = Field(default="", max_length=512)
    allowed_user_tags: list[str] = Field(default_factory=list)
    positive_keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    enabled: bool = True


class RegistryEnabledRequest(StrictBaseModel):
    enabled: bool
