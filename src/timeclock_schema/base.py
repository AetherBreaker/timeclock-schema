"""The base model and the timestamp, time-of-day and secret-reveal conventions every model shares."""

# Standard library imports
from datetime import UTC, time
from typing import Annotated, Final

# Third party imports
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict


class TimeclockModel(BaseModel):
  """Base of every model in the package.

  `extra="ignore"`: clocks and the backend can run different schema versions, so a newer migration's extra column must not
  break an older reader.
  """

  model_config = ConfigDict(frozen=True, extra="ignore")


def _reject_zone(value: time) -> time:
  if value.tzinfo is not None:
    raise ValueError("times of day carry no zone")
  return value


UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
"""An aware timestamp, stored in UTC. A naive one fails validation rather than being assumed local."""

LocalTime = Annotated[time, AfterValidator(_reject_zone)]
"""A naive time of day; callers interpret it in the configured local zone (R41.3, R41.4)."""

REVEAL: Final[dict[str, bool]] = {"reveal_secrets": True}
"""Serialization context that puts secret values in the output (`model_dump_json(context=REVEAL)`)."""
