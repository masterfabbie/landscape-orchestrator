"""Start, stop and restart of instances and systems, run in the background as activities.

Single instances are controlled through the host agent, which also works while the
sapstartsrv of the instance is down. Whole systems are controlled through StartSystem and
StopSystem of sapstartsrv, which already handles the order by start priority within one
system. Systems the target depends on (see :mod:`dependencies`) are started before it and
stopped after it. Progress is read from sapstartsrv until the target state is reached or
the timeout expires.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from .clients import SoapConnectionError, SoapError
from .clients.sapcontrol import StateColor, SystemInstance
from .connections import ClientFactory
from .crypto import SecretError
from .dependencies import SystemPlan, load_graph, plan
from .discovery import instance_status
from .models import (
    STATUS_UNKNOWN,
    Activity,
    ActivityLogEntry,
    Database,
    Instance,
    System,
    activity_systems,
)

logger = logging.getLogger(__name__)


class OperationKind(StrEnum):
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"


class TargetType(StrEnum):
    INSTANCE = "INSTANCE"
    SYSTEM = "SYSTEM"


class ActivityStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


ACTIVE_STATUSES = (ActivityStatus.PENDING, ActivityStatus.RUNNING)

DEFAULT_TIMEOUT_SECONDS = 600


class OperationConflictError(Exception):
    """Another activity is still running for the same system."""


class TargetNotFoundError(Exception):
    """The instance or system does not exist."""


class OperationFailedError(Exception):
    """The operation did not reach its target state."""


def _now() -> datetime:
    return datetime.now(UTC)


def _priority(instance: Instance) -> float:
    try:
        return float(instance.start_priority or "")
    except ValueError:
        return float("inf")


class OperationRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clients: ClientFactory,
        *,
        poll_interval: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._clients = clients
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._submit_lock = asyncio.Lock()

    # --- submitting ---------------------------------------------------------------------

    async def submit_instance(
        self,
        session: AsyncSession,
        instance_id: int,
        kind: OperationKind,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> Activity:
        instance = await session.scalar(
            select(Instance)
            .where(Instance.id == instance_id)
            .options(selectinload(Instance.system))
        )
        if instance is None:
            raise TargetNotFoundError("Instance not found")
        label = f"{instance.system.sid}/{instance.number}"
        return await self._submit(
            session,
            kind,
            TargetType.INSTANCE,
            label,
            instance.system_id,
            instance.id,
            timeout_seconds,
            {instance.system_id},
        )

    async def submit_system(
        self,
        session: AsyncSession,
        system_id: int,
        kind: OperationKind,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> Activity:
        system = await session.get(System, system_id)
        if system is None:
            raise TargetNotFoundError("System not found")
        system_plan = await self._plan(session, system.id)
        return await self._submit(
            session,
            kind,
            TargetType.SYSTEM,
            system.sid,
            system.id,
            None,
            timeout_seconds,
            system_plan.involved,
        )

    @staticmethod
    async def _plan(session: AsyncSession, root: int) -> SystemPlan:
        sids = dict((await session.execute(select(System.id, System.sid))).all())
        graph = await load_graph(session)
        return plan(root, graph, key=lambda system_id: sids.get(system_id, ""))

    async def _submit(
        self,
        session: AsyncSession,
        kind: OperationKind,
        target_type: TargetType,
        label: str,
        system_id: int,
        instance_id: int | None,
        timeout_seconds: int,
        involved: set[int],
    ) -> Activity:
        # One activity per system at a time: operations on a system and on its instances
        # would otherwise work against each other. A system operation also covers the
        # systems it depends on.
        async with self._submit_lock:
            active = await session.scalar(
                select(Activity)
                .outerjoin(activity_systems)
                .where(
                    Activity.status.in_(ACTIVE_STATUSES),
                    or_(
                        activity_systems.c.system_id.in_(involved),
                        Activity.system_id.in_(involved),
                    ),
                )
                .limit(1)
            )
            if active is not None:
                raise OperationConflictError(
                    f"Activity {active.id} ({active.kind} {active.target_label}) is still running"
                )
            activity = Activity(
                kind=kind,
                target_type=target_type,
                target_label=label,
                system_id=system_id,
                instance_id=instance_id,
                status=ActivityStatus.PENDING,
                timeout_seconds=timeout_seconds,
                requested_at=_now(),
            )
            activity.systems = list(
                await session.scalars(select(System).where(System.id.in_(involved)))
            )
            session.add(activity)
            await session.commit()
        activity_id = activity.id
        task = asyncio.create_task(self._run(activity_id), name=f"activity-{activity_id}")
        self._tasks[activity_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(activity_id, None))
        return activity

    # --- lifecycle ----------------------------------------------------------------------

    async def wait(self, activity_id: int) -> None:
        """Wait until the activity has finished. Mainly for tests."""
        task = self._tasks.get(activity_id)
        if task is not None:
            await asyncio.wait([task])

    async def recover_interrupted(self) -> int:
        """Mark activities left active by a previous server process as failed."""
        async with self._session_factory() as session:
            result = await session.execute(
                update(Activity)
                .where(Activity.status.in_(ACTIVE_STATUSES))
                .values(
                    status=ActivityStatus.FAILED,
                    finished_at=_now(),
                    error="Interrupted by a server restart. The state of the target is unknown.",
                )
            )
            await session.commit()
            return result.rowcount

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)

    # --- running ------------------------------------------------------------------------

    async def _run(self, activity_id: int) -> None:
        async with self._session_factory() as session:
            activity = await session.get(Activity, activity_id)
            if activity is None:
                return
            activity.status = ActivityStatus.RUNNING
            activity.started_at = _now()
            await session.commit()
            error: str | None = None
            try:
                await self._execute(session, activity)
            except asyncio.CancelledError:
                await self._finish(session, activity, "Interrupted by server shutdown.")
                raise
            except (OperationFailedError, SoapError, SecretError) as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - an activity must never stay "running"
                logger.exception("Activity %s failed unexpectedly", activity_id)
                error = f"Internal error: {exc!r}"
            try:
                await self._refresh_databases(session, activity)
            except (SoapError, SecretError):
                pass  # best effort; the next discovery corrects the status
            await self._finish(session, activity, error)

    async def _refresh_databases(self, session: AsyncSession, activity: Activity) -> None:
        """Update the stored status of the databases on the hosts the operation touched.

        Stopping a HANA instance stops its databases; without this the inventory would
        keep showing them as running until the next discovery. Hosts that cannot be
        queried are skipped.
        """
        query = select(Instance).options(selectinload(Instance.host))
        if activity.instance_id is not None:
            query = query.where(Instance.id == activity.instance_id)
        else:
            involved = select(activity_systems.c.system_id).where(
                activity_systems.c.activity_id == activity.id
            )
            query = query.where(
                or_(Instance.system_id == activity.system_id, Instance.system_id.in_(involved))
            )
        hosts = {instance.host.id: instance.host for instance in await session.scalars(query)}
        now = _now()
        for host in hosts.values():
            known = {
                (database.name, database.type): database
                for database in await session.scalars(
                    select(Database).where(Database.host_id == host.id)
                )
            }
            if not known:
                continue
            try:
                async with self._clients.hostagent(host) as agent:
                    reported = await agent.list_databases()
            except SoapError:
                continue
            for found in reported:
                database = known.get((found.name, found.type))
                if database is not None:
                    database.last_status = found.status.name
                    database.status_checked_at = now
        await session.commit()

    async def _finish(self, session: AsyncSession, activity: Activity, error: str | None) -> None:
        activity.finished_at = _now()
        if error is None:
            activity.status = ActivityStatus.SUCCEEDED
            session.add(self._entry(activity, "INFO", "Finished successfully"))
        else:
            activity.status = ActivityStatus.FAILED
            activity.error = error
            session.add(self._entry(activity, "ERROR", error))
        await session.commit()

    @staticmethod
    def _entry(activity: Activity, level: str, message: str) -> ActivityLogEntry:
        return ActivityLogEntry(
            activity_id=activity.id, timestamp=_now(), level=level, message=message
        )

    async def _log(self, session: AsyncSession, activity: Activity, message: str) -> None:
        session.add(self._entry(activity, "INFO", message))
        await session.commit()

    async def _execute(self, session: AsyncSession, activity: Activity) -> None:
        kind = OperationKind(activity.kind)
        phases = {
            OperationKind.START: (True,),
            OperationKind.STOP: (False,),
            OperationKind.RESTART: (False, True),
        }[kind]
        if activity.target_type == TargetType.INSTANCE:
            instance = await session.scalar(
                select(Instance)
                .where(Instance.id == activity.instance_id)
                .options(selectinload(Instance.host), selectinload(Instance.system))
            )
            if instance is None:
                raise OperationFailedError("The instance no longer exists")
            for start in phases:
                await self._instance_phase(session, activity, instance, start)
        else:
            if activity.system_id is None:
                raise OperationFailedError("The system no longer exists")
            system_plan = await self._plan(session, activity.system_id)
            instances_by_system = await self._instances_by_system(session, system_plan.involved)
            sids = dict(
                (
                    await session.execute(
                        select(System.id, System.sid).where(System.id.in_(system_plan.involved))
                    )
                ).all()
            )
            for start in phases:
                order = system_plan.start if start else system_plan.stop
                if len(system_plan.involved) > 1:
                    await self._log(
                        session,
                        activity,
                        f"{'Start' if start else 'Stop'} order including dependencies: "
                        + ", ".join(sids.get(s, f"#{s}") for s in order),
                    )
                if not start:
                    for kept, needed_by in sorted(system_plan.kept_running.items()):
                        await self._log(
                            session,
                            activity,
                            f"System {sids.get(kept, kept)} keeps running: also required by "
                            + ", ".join(sorted(sids.get(s, f"#{s}") for s in needed_by)),
                        )
                for system_id in order:
                    instances = instances_by_system.get(system_id, [])
                    if not instances:
                        raise OperationFailedError(
                            f"System {sids.get(system_id, f'#{system_id}')} has no instances "
                            "in the inventory"
                        )
                    await self._system_phase(session, activity, instances, start)

    @staticmethod
    async def _instances_by_system(
        session: AsyncSession, system_ids: set[int]
    ) -> dict[int, list[Instance]]:
        rows = await session.scalars(
            select(Instance)
            .where(Instance.system_id.in_(system_ids))
            .options(selectinload(Instance.host), selectinload(Instance.system))
        )
        grouped: dict[int, list[Instance]] = {system_id: [] for system_id in system_ids}
        for instance in rows:
            grouped[instance.system_id].append(instance)
        for instances in grouped.values():
            instances.sort(key=_priority)
        return grouped

    def _deadline_reached(self, deadline: float) -> bool:
        return self._clock() >= deadline

    # --- single instance ----------------------------------------------------------------

    async def _instance_phase(
        self, session: AsyncSession, activity: Activity, instance: Instance, start: bool
    ) -> None:
        host, system = instance.host, instance.system
        verb = "Start" if start else "Stop"
        target = StateColor.GREEN.name if start else StateColor.GRAY.name
        async with self._clients.hostagent(host) as agent:
            operation = agent.start_instance if start else agent.stop_instance
            await operation(system.sid, instance.number)
        await self._log(
            session, activity, f"{verb} of {activity.target_label} requested via host agent"
        )

        deadline = self._clock() + activity.timeout_seconds
        last: str | None = None
        while True:
            status = await self._read_instance_status(instance)
            if status != last:
                last = status
                instance.last_status = status
                instance.status_checked_at = _now()
                await self._log(session, activity, f"Instance status: {status}")
            if status == target:
                return
            if self._deadline_reached(deadline):
                raise OperationFailedError(
                    f"Timeout after {activity.timeout_seconds} s: instance is {status}, "
                    f"expected {target}"
                )
            await self._sleep(self._poll_interval)

    async def _read_instance_status(self, instance: Instance) -> str:
        """Status from sapstartsrv; UNKNOWN while it cannot be reached."""
        try:
            async with self._clients.sapcontrol(
                instance.host, int(instance.number), instance.system
            ) as sapcontrol:
                return instance_status(await sapcontrol.get_process_list())
        except SoapConnectionError:
            return STATUS_UNKNOWN

    # --- whole system -------------------------------------------------------------------

    async def _system_phase(
        self, session: AsyncSession, activity: Activity, instances: list[Instance], start: bool
    ) -> None:
        verb = "Start" if start else "Stop"
        target = StateColor.GREEN if start else StateColor.GRAY
        timeout = activity.timeout_seconds
        sid = instances[0].system.sid

        for instance in instances:
            try:
                async with self._clients.sapcontrol(
                    instance.host, int(instance.number), instance.system
                ) as sapcontrol:
                    if start:
                        await sapcontrol.start_system(waittimeout=timeout)
                    else:
                        await sapcontrol.stop_system(waittimeout=timeout)
            except SoapConnectionError:
                continue
            await self._log(
                session,
                activity,
                f"{verb} of system {sid} requested via sapstartsrv of "
                f"instance {instance.number} on {instance.host.name}",
            )
            break
        else:
            raise OperationFailedError(f"No sapstartsrv of system {sid} could be reached")

        deadline = self._clock() + timeout
        last: str | None = None
        while True:
            states = await self._read_system_states(instances)
            summary = (
                " ".join(f"{s.instance_nr:02d}:{s.dispstatus.name}" for s in states)
                if states
                else STATUS_UNKNOWN
            )
            if summary != last:
                last = summary
                self._store_states(instances, states)
                await self._log(session, activity, f"System {sid} status: {summary}")
            if states and all(state.dispstatus is target for state in states):
                return
            if self._deadline_reached(deadline):
                raise OperationFailedError(
                    f"Timeout after {timeout} s: system {sid} is {summary}, "
                    f"expected all {target.name}"
                )
            await self._sleep(self._poll_interval)

    async def _read_system_states(self, instances: list[Instance]) -> list[SystemInstance]:
        """Instance list of the system from the first sapstartsrv that answers."""
        for instance in instances:
            try:
                async with self._clients.sapcontrol(
                    instance.host, int(instance.number), instance.system
                ) as sapcontrol:
                    states = await sapcontrol.get_system_instance_list()
            except SoapConnectionError:
                continue
            return sorted(states, key=lambda state: state.instance_nr)
        return []

    @staticmethod
    def _store_states(instances: list[Instance], states: list[SystemInstance]) -> None:
        now = _now()
        by_number: dict[int, list[SystemInstance]] = {}
        for state in states:
            by_number.setdefault(state.instance_nr, []).append(state)
        for instance in instances:
            candidates = by_number.get(int(instance.number), [])
            match = next((c for c in candidates if c.hostname == instance.hostname), None)
            if match is None and len(candidates) == 1:
                match = candidates[0]
            if match is not None:
                instance.last_status = match.dispstatus.name
                instance.status_checked_at = now
