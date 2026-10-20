"""Live shopfloor bottleneck and queue monitoring (PRD 8.3).

Everything is calculated on demand from the operation timestamps production already
records (clock in/out, cycle seconds, hold timers). Nothing is stored, so the figures
cannot drift from the traveler record.
"""

from dataclasses import dataclass
from datetime import timedelta
from statistics import mean

from django.utils import timezone

from apps.plant.models import Machine
from apps.production.models import JobTraveler, ProductionRun, TravelerOperation

OP = TravelerOperation.Status

QUEUE_LIMIT = timedelta(hours=4)   # a lot waiting longer than this between stations is "stuck in queue"
SLOW_FACTOR = 1.5                  # running this much longer than the usual cycle is "slow"
MIN_SAMPLES = 3                    # completed runs needed before a usual cycle time means anything


@dataclass
class JobTiming:
    traveler: object
    state: str                     # RUNNING, WAITING, HOLD, DONE, BLOCKED
    elapsed: timedelta
    active: timedelta
    hold: timedelta
    queue: timedelta               # elapsed - active - hold: idle time between stations
    waiting_since: object = None
    waiting: timedelta = timedelta(0)
    stuck: bool = False
    current_operation: object = None


@dataclass
class MachineTile:
    machine: object
    status: str                    # IDLE, OK, SLOW
    operation: object = None
    running: timedelta = timedelta(0)
    usual: timedelta = None


def _paused(op):
    run = getattr(op, "run", None)
    return timedelta(seconds=run.paused_seconds) if run else timedelta(0)


def job_timing(traveler, now=None):
    """Elapsed, active machining, hold and idle queue time for one lot (PRD 8.3)."""
    now = now or timezone.now()
    ops = list(traveler.operations.select_related("routing_operation", "run").order_by("routing_operation__op_number"))
    finished = traveler.status != JobTraveler.Status.ACTIVE
    ends = [op.clock_out for op in ops if op.clock_out]
    end = (max(ends) if finished and ends else now)
    elapsed = max(timedelta(0), end - traveler.created_at)

    active = timedelta(0)
    hold = timedelta(0)
    for index, op in enumerate(ops):
        if op.status == OP.COMPLETED:
            active += timedelta(seconds=op.cycle_seconds or 0)
        elif op.status == OP.IN_PROGRESS and op.clock_in:
            active += max(timedelta(0), now - op.clock_in - _paused(op))
        if op.status == OP.COMPLETED and op.hold_until and op.clock_out:
            following = ops[index + 1].clock_in if index + 1 < len(ops) and ops[index + 1].clock_in else None
            hold += max(timedelta(0), min(op.hold_until, following or end) - op.clock_out)
    queue = max(timedelta(0), elapsed - active - hold)

    running = next((op for op in ops if op.status == OP.IN_PROGRESS), None)
    pending = next((op for op in ops if op.status == OP.PENDING), None)
    timing = JobTiming(traveler, "DONE", elapsed, active, hold, queue)
    if traveler.status == JobTraveler.Status.QUARANTINED:
        timing.state = "BLOCKED"
    elif traveler.status != JobTraveler.Status.ACTIVE:
        timing.state = "DONE"
    elif running:
        timing.state, timing.current_operation = "RUNNING", running
    elif pending:
        timing.current_operation = pending
        previous = [op for op in ops if op.status == OP.COMPLETED]
        last = previous[-1] if previous else None
        since = traveler.created_at
        if last:
            since = max(last.clock_out, last.hold_until or last.clock_out)
        if last and last.hold_until and last.hold_until > now:
            timing.state = "HOLD"
        else:
            timing.state, timing.waiting_since = "WAITING", since
            timing.waiting = max(timedelta(0), now - since)
            timing.stuck = timing.waiting > QUEUE_LIMIT
    return timing


def usual_cycle(routing_operation):
    """Average active time of the completed runs of this routing step, or None when there are too few."""
    seconds = list(
        TravelerOperation.objects.filter(
            routing_operation=routing_operation, status=OP.COMPLETED, cycle_seconds__isnull=False
        ).values_list("cycle_seconds", flat=True)
    )
    if len(seconds) < MIN_SAMPLES:
        return None
    return timedelta(seconds=mean(seconds))


def machine_tiles(now=None):
    """One tile per active machine: idle, running normally, or running slower than usual (PRD 8.3)."""
    now = now or timezone.now()
    tiles = []
    for machine in Machine.objects.filter(is_active=True).select_related("department"):
        op = (TravelerOperation.objects.select_related("routing_operation", "traveler__work_order", "run")
              .filter(machine=machine, status=OP.IN_PROGRESS, traveler__status=JobTraveler.Status.ACTIVE).first())
        if op is None:
            tiles.append(MachineTile(machine, "IDLE"))
            continue
        running = max(timedelta(0), now - op.clock_in - _paused(op))
        usual = usual_cycle(op.routing_operation)
        slow = usual is not None and running > usual * SLOW_FACTOR
        tiles.append(MachineTile(machine, "SLOW" if slow else "OK", op, running, usual))
    return tiles


def floor_report(now=None):
    """Everything the supervisor's monitor shows: lots with timings, stuck lots and machine tiles."""
    now = now or timezone.now()
    lots = [job_timing(t, now) for t in
            JobTraveler.objects.filter(status=JobTraveler.Status.ACTIVE).select_related("work_order__part")]
    lots.sort(key=lambda j: j.waiting, reverse=True)
    return {
        "lots": lots,
        "stuck": [j for j in lots if j.stuck],
        "tiles": machine_tiles(now),
    }
