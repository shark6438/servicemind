from html.parser import HTMLParser

from pydantic import BaseModel, ConfigDict, Field


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = data.strip()
        if value:
            self.parts.append(value)


def html_to_text(value: str) -> str:
    """Convert GLPI rich-text fields into bounded plain text for model context."""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return " ".join(parser.parts)


class GlpiReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str | None = None


class GlpiTeamMember(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    name: str | None = None
    type: str | None = None
    role: str | None = None


class GlpiTicket(BaseModel):
    """Stable subset of the GLPI Ticket schema used by ServiceMind."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    content: str = ""
    type: int | None = None
    urgency: int | None = Field(default=None, ge=1, le=5)
    impact: int | None = Field(default=None, ge=1, le=5)
    priority: int | None = Field(default=None, ge=1, le=5)
    external_id: str | None = None
    status: GlpiReference | None = None
    entity: GlpiReference | None = None
    team: list[GlpiTeamMember] = Field(default_factory=list)

    def to_agent_payload(self) -> dict[str, object]:
        """Return a compact, sanitized payload instead of raw GLPI JSON/HTML."""
        return {
            "id": self.id,
            "name": self.name,
            "content": html_to_text(self.content),
            "type": self.type,
            "urgency": self.urgency,
            "impact": self.impact,
            "priority": self.priority,
            "external_id": self.external_id,
            "status": self.status.model_dump() if self.status else None,
            "entity": self.entity.model_dump() if self.entity else None,
            "team": [member.model_dump() for member in self.team],
        }


class GlpiFollowup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    itemtype: str | None = None
    items_id: int | None = None
    content: str = ""
    is_private: bool = False

    def to_agent_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ticket_id": self.items_id,
            "content": html_to_text(self.content),
            "is_private": self.is_private,
        }


class GlpiGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str
    entity: GlpiReference | None = None

    def to_agent_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "entity": self.entity.model_dump() if self.entity else None,
        }


class GlpiKnowbaseItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    answer: str = ""
    is_faq: bool = False
    begin_date: str | None = None
    end_date: str | None = None
    date_mod: str | None = None
    entity: GlpiReference | None = None
    entities: list[GlpiReference] = Field(default_factory=list)
    groups: list[GlpiReference] = Field(default_factory=list)
    profiles: list[GlpiReference] = Field(default_factory=list)
    users: list[GlpiReference] = Field(default_factory=list)

    def to_agent_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "answer": html_to_text(self.answer),
            "answer_html": self.answer,
            "is_faq": self.is_faq,
            "begin_date": self.begin_date,
            "end_date": self.end_date,
            "entity_ids": [x.id for x in self.entities]
            or ([self.entity.id] if self.entity else []),
            "group_ids": [x.id for x in self.groups],
            "profile_ids": [x.id for x in self.profiles],
            "user_ids": [str(x.id) for x in self.users],
        }


class GlpiTokenResponse(BaseModel):
    token_type: str
    expires_in: int
    access_token: str
