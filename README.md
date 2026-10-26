# timeclock-schema

The data shapes shared by [timeclock-system](https://github.com/AetherBreaker/timeclock-system) (the clock) and
[timeclock-backend](https://github.com/AetherBreaker/timeclock-backend): pydantic models, the shift logic, the
rules registry, and the PostgreSQL migrations the backend runs on startup. Published to SFTPyPI; both
projects depend on it by version. It's also a submodule of timeclock-system at `schema/`, for editing.
