"""How much LinkedIn each signed-in session (mock / main) may still be asked today.

Every LinkedIn caller (curator people search, lead scout, employer-link lookup) goes through this:
a daily count per session and a cooldown after a 429, checkpoint or login wall, so a throttled
session is left alone and the next caller goes to the other one or waits. Keyed by the session's
cookie jar, so the owner, a member on 'inherit' and an install-level pass share one budget, and a
member with their own sessions has their own.

The day's count lives in the database (table li_usage) and is the only copy. `take()` reserves a
load with one atomic statement per counter (`INSERT ... ON CONFLICT DO UPDATE ... WHERE n < cap`),
so threads and processes cannot both pass a check-then-increment race, and the daily total and the
run's own counter are reserved in one transaction: a refusal on either leaves neither incremented.
It fails CLOSED: when the count cannot be read or the load cannot be recorded, the load is refused
and the error is logged, because a load that could not be recorded could be granted again after a
restart. The cooldown after a 429 stays process-local (a restart means a fresh look at the session).

Per-run share: with curate_runs_per_day >= 2 the curator's scheduled runs split each session's daily
cap by `linkedin_run_split` (default "8,5") with `linkedin_run_reserve` (default 2) held back. The
numbers are weights: the main session (15) gives exactly 8 / 5 / 2, the mock session (60) 32 / 20 / 8.
 * Run N (claimed atomically, 1 = first of the day) may take only its own share. A run beyond the
   planned ones gets NO share of its own: it is refused, so the reserve is never eaten.
 * A manual "Run now" does not claim an ordinal, so it cannot shift the scheduled runs' shares and
   never takes one: it may use only what the plan leaves over (the reserve and rounding), counted in
   the "reserve" bucket it shares with the lead scout, and still within the daily total.
 * A crashed run keeps the ordinal it claimed and what it loaded stays counted; its unspent part is
   not handed to a later run (conservative). A restart of the day's runs counts as the next run.
 * Callers outside a run (lead scout, link lookup) draw on the daily total only; the total is never
   exceeded under any setting combination. With one run a day there is no split.
"""
import contextvars
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from backend.tz import local_today

logger = logging.getLogger("jobnavigator.curator.linkedin")

MOCK, MAIN = "mock", "main"
DEFAULT_CAP = {MOCK: 60, MAIN: 15}
DEFAULT_COOLDOWN_HOURS = 6

DEFAULT_RUN_SPLIT = "8,5"
DEFAULT_RUN_RESERVE = 2

_OFF: set = set()    # (day, kind) of faceted query types LinkedIn ignored, this process
_COOL: dict = {}     # jar -> aware datetime the cooldown ends
_RUN: contextvars.ContextVar = contextvars.ContextVar("li_budget_run", default=None)   # (ordinal, shares) of this run


def _jar(which: str) -> str:
    from backend import refresh_linkedin_session as rls
    return rls.jar(which)


def _setting_int(key: str, default: int) -> int:
    from backend.analyzer.llm_client import _get_setting
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        try:
            return max(0, int(_get_setting(db, key, str(default))))
        except (TypeError, ValueError):
            return default
    finally:
        db.close()


def cap(which: str) -> int:
    return _setting_int(f"linkedin_{which}_daily_cap", DEFAULT_CAP[which])


_RESERVE_SQL = text(
    "INSERT INTO li_usage (jar, day, bucket, n) VALUES (:jar, :day, :bucket, 1) "
    "ON CONFLICT (jar, day, bucket) DO UPDATE SET n = li_usage.n + 1 WHERE li_usage.n < :cap")
_CLAIM_SQL = text(
    "INSERT INTO li_usage (jar, day, bucket, n) VALUES (:jar, :day, :bucket, 1) "
    "ON CONFLICT (jar, day, bucket) DO UPDATE SET n = li_usage.n + 1")


def _db_read(jar: str, day, bucket: str):
    from backend.models.db import LiUsage, SessionLocal
    db = SessionLocal()
    try:
        row = db.get(LiUsage, (jar, day, bucket))
        return row.n if row else 0
    finally:
        db.close()


def _reserve(jar: str, day, limits: list) -> bool:
    """Atomically take one load on every (bucket, cap) of `limits`, in one transaction. False (and
    nothing counted) when any is at its cap. Raises when the database cannot record it."""
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        for bucket, lim in limits:
            if lim <= 0:
                db.rollback()
                return False
            done = db.execute(_RESERVE_SQL, {"jar": jar, "day": day, "bucket": bucket, "cap": lim})
            if (done.rowcount or 0) < 1:
                db.rollback()
                return False
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claim(jar: str, day, bucket: str) -> int:
    """Atomically add one to a counter and return its new value (one transaction)."""
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        db.execute(_CLAIM_SQL, {"jar": jar, "day": day, "bucket": bucket})
        n = db.execute(text("SELECT n FROM li_usage WHERE jar = :jar AND day = :day AND bucket = :bucket"),
                       {"jar": jar, "day": day, "bucket": bucket}).scalar()
        db.commit()
        return int(n)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _count(which: str, bucket: str = "all"):
    """Loads counted today on this session's bucket; None when the database cannot be read (callers
    then treat the budget as spent)."""
    try:
        return _db_read(_jar(which), local_today(), bucket)
    except Exception as e:
        logger.error(f"LinkedIn usage cannot be read from the database ({e}); refusing LinkedIn loads (fail closed)")
        return None


def used(which: str) -> int:
    n = _count(which)
    return cap(which) if n is None else n          # unreadable: shown (and treated) as spent


def _setting_str(key: str, default: str) -> str:
    from backend.analyzer.llm_client import _get_setting
    from backend.models.db import SessionLocal
    db = SessionLocal()
    try:
        return str(_get_setting(db, key, default) or default)
    finally:
        db.close()


def run_shares(which: str, runs_per_day: int) -> list:
    """Page loads each of the day's curator runs may use on this session; [] = no split (one run a day).

    The split and the reserve are weights over the session's cap; the shares never add up to
    more than cap minus the share held back."""
    if runs_per_day < 2:
        return []
    try:
        split = [max(0, int(x)) for x in _setting_str("linkedin_run_split", DEFAULT_RUN_SPLIT).split(",") if x.strip()]
    except ValueError:
        split = [int(x) for x in DEFAULT_RUN_SPLIT.split(",")]
    reserve = _setting_int("linkedin_run_reserve", DEFAULT_RUN_RESERVE)
    split = (split or [int(x) for x in DEFAULT_RUN_SPLIT.split(",")])[:runs_per_day]
    total = sum(split) + reserve
    if not total:
        return []
    c = cap(which)
    return [c * w // total for w in split]       # floor: the rounding can only shrink a share


NOT_PLANNED = 10 ** 6       # ordinal of a run that could not be claimed: it gets no share
MANUAL = -1                 # ordinal of a manual "Run now": it claims nothing and draws on the leftover only
SCOUT = -2                  # the lead scout: it draws on the reserve only, in a bucket of its own


def claim_run(runs_per_day: int, manual: bool = False) -> int:
    """A curator run starts. Returns its ordinal: 1, 2, ... for scheduled runs, claimed atomically in
    the database (two starts never get the same number, and a restart keeps the count); MANUAL for a
    manual run, which claims nothing; 0 with one run a day, when there is no split. If the claim
    cannot be recorded the run gets NOT_PLANNED (no share, fail closed).
    Blocking; call it from a thread. The caller then calls enter_run in its own task, because a
    context variable set inside a worker thread does not reach the task that started it."""
    if runs_per_day < 2:
        return 0
    if manual:
        return MANUAL
    try:
        return _claim(_jar(MAIN), local_today(), "_runs")
    except Exception as e:
        logger.error(f"curator run could not be recorded ({e}); it gets no LinkedIn share (fail closed)")
        return NOT_PLANNED


def enter_run(ordinal: int, runs_per_day: int) -> None:
    """Give the current task (and what it awaits) the limits of run `ordinal`; 0 = no split."""
    _RUN.set((ordinal, runs_per_day) if ordinal else None)


def begin_run(runs_per_day: int, manual: bool = False) -> int:
    ordinal = claim_run(runs_per_day, manual)
    enter_run(ordinal, runs_per_day)
    return ordinal


def begin_scout(runs_per_day: int) -> None:
    """The lead scout starts: with a split (2 or more curator runs a day) it may use only the reserve the runs
    leave over, so it cannot eat the loads the runs plan on. One run a day: no split, daily total only."""
    enter_run(SCOUT, runs_per_day)


def end_run() -> None:
    _RUN.set(None)


def _run_limit(which: str):
    """(bucket, loads this run may take in all today on `which`) or (None, None) outside a split run."""
    cur = _RUN.get()
    if not cur:
        return None, None
    ordinal, rpd = cur
    shares = run_shares(which, rpd)
    if not shares:
        return None, None
    if ordinal == SCOUT:
        return "reserve", max(0, cap(which) - sum(shares))      # the reserve, not the curator runs' loads
    if ordinal == MANUAL:
        return "reserve", max(0, cap(which) - sum(shares))      # only what no scheduled run owns: one bucket shared
                                                                # with the scout, so together they never take more
    if 1 <= ordinal <= len(shares):
        return f"run{ordinal}", shares[ordinal - 1]
    return f"run{ordinal}", 0                                   # beyond the plan: no share of its own


def remaining(which: str) -> int:
    """Loads this caller (the current run, the scout, or nobody in particular) may still take on `which` today:
    the smaller of the daily total's and its own share's leftover; 0 while cooling or when usage is unreadable."""
    if cooling_until(which) is not None:
        return 0
    total = _count(which)
    if total is None:
        return 0
    left = max(0, cap(which) - total)
    bucket, limit = _run_limit(which)
    if bucket is None:
        return left
    mine = _count(which, bucket)
    return 0 if mine is None else min(left, max(0, limit - mine))


# A kind of faceted query LinkedIn ignored today (curator/linkedin_people.py): recorded in li_usage under the
# bucket "off:<kind>" so a restart does not forget it, and the day's rollover clears it.
def _off_bucket(kind: str) -> str:
    return f"off:{kind}"


def mark_unusable(kind: str) -> None:
    try:
        _claim(_jar(MAIN), local_today(), _off_bucket(kind))
    except Exception as e:
        logger.error(f"could not record that {kind!r} queries are unusable today ({e})")
    _OFF.add((local_today(), kind))


def unusable(kind: str) -> bool:
    if (local_today(), kind) in _OFF:
        return True
    try:
        return _db_read(_jar(MAIN), local_today(), _off_bucket(kind)) > 0
    except Exception:
        return True          # unreadable: do not spend on a query type that may be dropped


def cooling_until(which: str):
    until = _COOL.get(_jar(which))
    if until and until <= datetime.now(timezone.utc):
        _COOL.pop(_jar(which), None)
        return None
    return until


def available(which: str) -> bool:
    """Whether a load could be taken now (not reserved: use take() for that)."""
    if cooling_until(which) is not None:
        return False
    total = _count(which)
    if total is None or total >= cap(which):
        return False
    bucket, limit = _run_limit(which)
    if bucket is None:
        return True
    mine = _count(which, bucket)
    return mine is not None and mine < limit


def take(which: str) -> bool:
    """Count one load on this session; False (nothing counted) when it is cooling, spent, over this
    run's share, or when the database cannot record it."""
    if cooling_until(which) is not None or _count(which) is None:       # unreadable usage: fail closed
        return False
    limits = [("all", cap(which))]
    bucket, share = _run_limit(which)
    if bucket is not None:
        limits.append((bucket, share))
    try:
        return _reserve(_jar(which), local_today(), limits)
    except Exception as e:
        logger.error(f"LinkedIn load not recorded in the database ({e}); refusing it (fail closed)")
        return False


def cool(which: str, why: str = "rate-limited") -> None:
    hours = _setting_int("linkedin_cooldown_hours", DEFAULT_COOLDOWN_HOURS)
    _COOL[_jar(which)] = datetime.now(timezone.utc) + timedelta(hours=hours)
    logger.warning(f"LinkedIn {which} session {why}: left alone for {hours} h")


def status() -> dict:
    """{mock|main: {used, cap, cooling_until}} for the current account's sessions."""
    out = {}
    for which in (MOCK, MAIN):
        until = cooling_until(which)
        out[which] = {"used": used(which), "cap": cap(which),
                      "cooling_until": until.isoformat() if until else None}
    return out


def reset_all() -> None:
    """Tests: forget every count and cooldown."""
    _COOL.clear()
    _OFF.clear()
    _RUN.set(None)
    try:
        from backend.models.db import LiUsage, SessionLocal
        db = SessionLocal()
        try:
            db.query(LiUsage).delete()
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def forget_process_state() -> None:
    """Tests: what an app restart loses (memory only); the day's count in the database stays."""
    _COOL.clear()
    _OFF.clear()
    _RUN.set(None)
