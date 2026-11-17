"""Start, stop and restart of instances and systems, run in the background as activities.

All starts and stops go through the host agent (StartInstance, StopInstance), which needs
only the host credentials (sapadm). sapstartsrv rejects StartSystem and StopSystem for
sapadm on real systems, so whole systems are orchestrated here. Their instances may belong
to several SIDs; database and central services start in parallel, application servers
after them, and stops run the other way round (see :mod:`roles`). Diagnostics agents are
left out.
Systems the target depends on (see :mod:`dependencies`) are started before it and stopped
after it. Progress is read from sapstartsrv (GetProcessList, allowed for sapadm) until the
target state is reached or the timeout expires.
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

from .clients import (
    AuthenticationRequiredError,
    InvalidCredentialsError,
    PermissionDeniedError,
    SoapConnectionError,
    SoapError,
    SoapFaultError,
)
from .clients.sapcontrol import StateColor
from .connections import ClientFactory
from .crypto import SecretError
from .dependencies import SystemPlan, load_graph, plan
from .discovery import instance_status
from .models import (
    STATUS_UNKNOWN,
    Activity,
    ActivityLogEntry,
    Database,
    Host,
    Instance,
    System,
    activity_systems,
)
from .roles import Role

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


class NotAssignedError(Exception):
    """The instance is not assigned to a system and cannot be operated."""


class OperationFailedError(Exception):
    """The operation did not reach its target state."""


def explain_fault(
    exc: SoapFaultError, call: str, host: Host, instance: Instance | None = None
) -> OperationFailedError:
    """Turn a SOAP fault into an error that says what was rejected and what to do.

    ``instance`` set means the call went to the sapstartsrv of that instance, otherwise to
    the host agent of ``host``.
    """
    if instance is None:
        where = f"the host agent of {host.name}"
        user = host.username
        used = f"the credentials of host {host.name} ({user})" if user else "no credentials"
        advice = (
            f"Check the credentials of host {host.name}; the host agent usually expects sapadm."
        )
    else:
        sid = instance.sid
        where = f"the sapstartsrv of instance {instance.label} on {host.name}"
        admin = f"{sid.name.lower()}adm"
        if sid.has_credentials:
            used = f"the credentials of SID {sid.name} ({sid.username})"
            advice = f"Administrative calls at sapstartsrv need a user such as {admin}."
        else:
            user = host.username
            used = (
                f"no credentials of SID {sid.name}, so those of host {host.name} ({user})"
                if user
                else "no credentials"
            )
            place = (
                f"in the configuration of system {instance.system.name}"
                if instance.system is not None
                else "in the configuration of its system"
            )
            advice = (
                f"Administrative calls at sapstartsrv need a user such as {admin}: "
                f"store credentials for SID {sid.name} {place}."
            )

    if isinstance(exc, AuthenticationRequiredError):
        reason = "credentials are required"
    elif isinstance(exc, InvalidCredentialsError):
        reason = "user or password are wrong"
    elif isinstance(exc, PermissionDeniedError):
        reason = "the user is not authorized"
    else:
        return OperationFailedError(f"{call} at {where} failed: {exc.faultstring}")
    return OperationFailedError(
        f"{call} at {where} was rejected ({exc.faultstring}): {reason}. "
        f"The request used {used}. {advice}"
    )


def _now() -> datetime:
    return datetime.now(UTC)


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
            .options(selectinload(Instance.sid))
        )
        if instance is None:
            raise TargetNotFoundError("Instance not found")
        if instance.system_id is None:
            raise NotAssignedError(f"Instance {instance.label} is not assigned to a system")
        return await self._submit(
            session,
            kind,
            TargetType.INSTANCE,
            instance.label,
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
            system.name,
            system.id,
            None,
            timeout_seconds,
            system_plan.involved,
        )

    @staticmethod
    async def _plan(session: AsyncSession, root: int) -> SystemPlan:
        names = dict((await session.execute(select(System.id, System.name))).all())
        graph = await load_graph(session)
        return plan(root, graph, key=lambda system_id: names.get(system_id, ""))

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
                .options(
                    selectinload(Instance.host),
                    selectinload(Instance.sid),
                    selectinload(Instance.system),
                )
            )
            if instance is None:
                raise OperationFailedError("The instance no longer exists")
            if instance.system_id is None:
                raise OperationFailedError("The instance is no longer assigned to a system")
            for start in phases:
                await self._instance_phase(session, activity, instance, start)
        else:
            if activity.system_id is None:
                raise OperationFailedError("The system no longer exists")
            system_plan = await self._plan(session, activity.system_id)
            instances_by_system = await self._instances_by_system(session, system_plan.involved)
            names = dict(
                (
                    await session.execute(
                        select(System.id, System.name).where(
                            System.id.in_(system_plan.involved)
                        )
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
                        + ", ".join(names.get(s, f"#{s}") for s in order),
                    )
                if not start:
                    for kept, needed_by in sorted(system_plan.kept_running.items()):
                        await self._log(
                            session,
                            activity,
                            f"System {names.get(kept, kept)} keeps running: also required by "
                            + ", ".join(sorted(names.get(s, f"#{s}") for s in needed_by)),
                        )
                for system_id in order:
                    instances = instances_by_system.get(system_id, [])
                    if not instances:
                        raise OperationFailedError(
                            f"System {names.get(system_id, f'#{system_id}')} has no instances "
                            "in the inventory that are started or stopped with the system"
                        )
                    name = names.get(system_id, f"#{system_id}")
                    await self._system_phase(session, activity, name, instances, start)

    @staticmethod
    async def _instances_by_system(
        session: AsyncSession, system_ids: set[int]
    ) -> dict[int, list[Instance]]:
        rows = await session.scalars(
            select(Instance)
            .where(Instance.system_id.in_(system_ids))
            .options(
                selectinload(Instance.host),
                selectinload(Instance.sid),
                selectinload(Instance.system),
            )
        )
        grouped: dict[int, list[Instance]] = {system_id: [] for system_id in system_ids}
        for instance in rows:
            # Diagnostics agents belong to the system but are not started or stopped with it.
            if instance.system_id is not None and not instance.is_agent:
                grouped[instance.system_id].append(instance)
        for instances in grouped.values():
            instances.sort(key=lambda instance: instance.priority)
        return grouped

    def _deadline_reached(self, deadline: float) -> bool:
        return self._clock() >= deadline

    # --- single instance ----------------------------------------------------------------

    async def _instance_phase(
        self, session: AsyncSession, activity: Activity, instance: Instance, start: bool
    ) -> None:
        target = StateColor.GREEN.name if start else StateColor.GRAY.name
        await self._request_via_hostagent(session, activity, instance, start)

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
                instance.host, int(instance.number), instance.sid
            ) as sapcontrol:
                return instance_status(await sapcontrol.get_process_list())
        except SoapConnectionError:
            return STATUS_UNKNOWN
        except SoapFaultError as exc:
            raise explain_fault(exc, "GetProcessList", instance.host, instance) from exc

    # --- whole system -------------------------------------------------------------------

    @staticmethod
    def _phases(instances: list[Instance], start: bool) -> list[list[list[list[Instance]]]]:
        """Phases of a system operation; each phase is a list of tracks run in parallel.

        A track is a list of priority groups handled one after another. Database and central
        services form one phase with two parallel tracks, application servers another:

        - start: database and central services, then application servers,
        - stop: application servers, then central services and database.

        Within a track the start priority orders the groups (ascending for a start).
        """

        def track(role: Role) -> list[list[Instance]]:
            levels: dict[float, list[Instance]] = {}
            for instance in instances:
                if instance.role is role:
                    levels.setdefault(instance.priority, []).append(instance)
            return [levels[p] for p in sorted(levels, reverse=not start)]

        if start:
            phases = [[track(Role.DATABASE), track(Role.CENTRAL)], [track(Role.APPLICATION)]]
        else:
            phases = [[track(Role.APPLICATION)], [track(Role.CENTRAL), track(Role.DATABASE)]]
        return [[t for t in phase if t] for phase in phases if any(phase)]

    async def _system_phase(
        self,
        session: AsyncSession,
        activity: Activity,
        name: str,
        instances: list[Instance],
        start: bool,
    ) -> None:
        """Start or stop all instances of one system through their host agents.

        The order follows the roles of the instances (see :meth:`_phases`). Each priority
        group must reach the target state before the next group of its track begins, and
        each phase must be complete before the next phase begins. Instances already in the
        target state are skipped. This needs only the host credentials (sapadm);
        sapstartsrv is only read. Diagnostics agents are not passed in.
        """
        target = StateColor.GREEN.name if start else StateColor.GRAY.name
        timeout = activity.timeout_seconds
        deadline = self._clock() + timeout

        statuses = await self._read_statuses(instances)
        last = self._summary(instances, statuses)
        self._store_statuses(instances, statuses)
        await self._log(session, activity, f"System {name} status: {last}")
        if all(statuses[i.id] == target for i in instances):
            state = "running" if start else "stopped"
            await self._log(session, activity, f"System {name} is already {state}")
            return

        for tracks in self._phases(instances, start):
            positions = [0] * len(tracks)
            requested = [False] * len(tracks)
            while True:
                for index, track in enumerate(tracks):
                    while positions[index] < len(track):
                        group = track[positions[index]]
                        if not requested[index]:
                            for instance in group:
                                if statuses[instance.id] != target:
                                    await self._request_via_hostagent(
                                        session, activity, instance, start
                                    )
                            requested[index] = True
                        if not all(statuses[i.id] == target for i in group):
                            break
                        positions[index] += 1
                        requested[index] = False
                if all(positions[i] == len(track) for i, track in enumerate(tracks)):
                    break
                if self._deadline_reached(deadline):
                    raise OperationFailedError(
                        f"Timeout after {timeout} s: system {name} is {last}, "
                        f"expected all {target}"
                    )
                await self._sleep(self._poll_interval)
                statuses = await self._read_statuses(instances)
                summary = self._summary(instances, statuses)
                if summary != last:
                    last = summary
                    self._store_statuses(instances, statuses)
                    await self._log(session, activity, f"System {name} status: {summary}")

    async def _request_via_hostagent(
        self, session: AsyncSession, activity: Activity, instance: Instance, start: bool
    ) -> None:
        verb, call = ("Start", "StartInstance") if start else ("Stop", "StopInstance")
        label = instance.label
        try:
            async with self._clients.hostagent(instance.host) as agent:
                operation = agent.start_instance if start else agent.stop_instance
                await operation(instance.sid.name, instance.number, hostname=instance.hostname)
        except SoapFaultError as exc:
            raise explain_fault(exc, call, instance.host) from exc
        except SoapConnectionError as exc:
            raise OperationFailedError(
                f"{call} for {label}: the host agent of {instance.host.name} "
                f"could not be reached ({exc})"
            ) from exc
        await self._log(
            session,
            activity,
            f"{verb} of {label} requested via host agent of {instance.host.name}",
        )

    async def _read_statuses(self, instances: list[Instance]) -> dict[int, str]:
        return {instance.id: await self._read_instance_status(instance) for instance in instances}

    @staticmethod
    def _summary(instances: list[Instance], statuses: dict[int, str]) -> str:
        ordered = sorted(instances, key=lambda instance: (instance.sid.name, instance.number))
        return " ".join(f"{i.label}:{statuses[i.id]}" for i in ordered)

    @staticmethod
    def _store_statuses(instances: list[Instance], statuses: dict[int, str]) -> None:
        now = _now()
        for instance in instances:
            instance.last_status = statuses[instance.id]
            instance.status_checked_at = now
