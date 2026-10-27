"""The alert system's shared half (spec §6.11): alert types and their hooks, registered in code.

Three extension points, each independent of the others: alert types (any condition), actions (delivery channels, named
here and handled by the side that delivers them: the clock's confirmation notice, later the backend's SMS and email), and
hooks binding a type, plus a lead time for a threshold type, to actions. Measures read totals from
`shifts.total_hours` and compute none themselves. Version 1 registers one threshold type and one hook; event and
scheduled types join `AlertType` when they arrive.
"""

# Standard library imports
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Final

# First party imports
from timeclock_schema.shifts import total_hours, week_bounds

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Iterable, Sequence
  from datetime import datetime
  from zoneinfo import ZoneInfo

  # First party imports
  from timeclock_schema.models import Punch, PunchCorrection, Settings

WEEKLY_HOURS: Final = "weekly_hours"
CONFIRMATION_NOTICE: Final = "confirmation_notice"
"""The clock's action: a line on the confirmation screen after a punch."""


@dataclass(frozen=True)
class AlertContext:
  """What a measure sees: one employee's punches (enough to cover the measure's range), the settings and the moment."""

  employee_id: int
  punches: Sequence[Punch]
  corrections: Sequence[PunchCorrection]
  settings: Settings
  tz: ZoneInfo
  now: datetime

  def __post_init__(self) -> None:
    """Reject a naive `now` and store it in UTC.

    Raises:
      ValueError: `now` is naive.
    """
    if self.now.utcoffset() is None:
      raise ValueError("naive datetime; pass an aware one")
    object.__setattr__(self, "now", self.now.astimezone(UTC))


@dataclass(frozen=True)
class ThresholdType:
  """An alert on a measure reaching a limit; hooks on it may fire a lead time early."""

  key: str
  measure: Callable[[AlertContext], timedelta]
  limit: Callable[[Settings], timedelta]


type AlertType = ThresholdType


@dataclass(frozen=True)
class Hook:
  """Binds an alert type to actions; for a threshold type, fires once the measure is within `lead` of the limit."""

  alert: str
  lead: timedelta
  actions: tuple[str, ...]


@dataclass(frozen=True)
class Firing:
  """A hook whose threshold the measure has reached, with the values its actions report."""

  hook: Hook
  measure: timedelta
  limit: timedelta


ALERT_TYPES: Final[dict[str, AlertType]] = {}
HOOKS: Final[list[Hook]] = []


def register_type(alert_type: AlertType) -> None:
  """Register an alert type.

  Raises:
    ValueError: Its key is already registered.
  """
  if alert_type.key in ALERT_TYPES:
    raise ValueError(f"alert type {alert_type.key!r} is already registered")
  ALERT_TYPES[alert_type.key] = alert_type


def register_hook(hook: Hook) -> None:
  """Register a hook.

  Raises:
    ValueError: Its alert type is unknown, or its lead is negative.
  """
  if hook.alert not in ALERT_TYPES:
    raise ValueError(f"unknown alert type {hook.alert!r}")
  if hook.lead < timedelta():
    raise ValueError("a hook's lead can't be negative")
  HOOKS.append(hook)


def fired_hooks(ctx: AlertContext, hooks: Iterable[Hook] = HOOKS) -> tuple[Firing, ...]:
  """Return every hook whose threshold (the limit less its lead) `ctx`'s measure has reached, in hook order.

  Each type is measured once however many hooks it has. Never blocks anything: the caller only delivers the firings.
  """
  measures: dict[str, timedelta] = {}
  firings: list[Firing] = []
  for hook in hooks:
    alert_type = ALERT_TYPES[hook.alert]
    if hook.alert not in measures:
      measures[hook.alert] = alert_type.measure(ctx)
    limit = alert_type.limit(ctx.settings)
    if measures[hook.alert] >= limit - hook.lead:
      firings.append(Firing(hook, measures[hook.alert], limit))
  return tuple(firings)


def _weekly_hours(ctx: AlertContext) -> timedelta:
  # Worked plus paid break time, Monday to Sunday local (R33): the same figure the clock shows as "hours this week".
  start, end = week_bounds(ctx.now, ctx.tz)
  totals = total_hours(ctx.punches, ctx.corrections, settings=ctx.settings, tz=ctx.tz, start=start, end=end, now=ctx.now)
  return totals.worked + totals.paid_break


# The setting has two decimal places, so whole seconds hold it exactly; no float.
register_type(ThresholdType(WEEKLY_HOURS, _weekly_hours, lambda s: timedelta(seconds=int(s.weekly_hours_alert * 3600))))
register_hook(Hook(WEEKLY_HOURS, timedelta(), (CONFIRMATION_NOTICE,)))
