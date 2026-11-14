from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator


class ReportFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: UUID | None = None
    api_key_id: UUID | None = None
    model_id: UUID | None = None
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    time_basis: Literal["occurred_at", "posted_at"] = "occurred_at"

    @model_validator(mode="after")
    def ordered(self):
        if self.start and self.end and self.end <= self.start:
            raise ValueError("end must follow start")
        return self


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["requests", "charges", "ledger", "attempt_costs"]
    filters: ReportFilter = ReportFilter()
