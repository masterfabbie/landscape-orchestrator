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
import json
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
from .clients.hostagent import FAULT_TIMEOUT, OperationFault
from .clients.sapcontrol import StateColor
from .connections import ClientFactory
from .crypto import SecretError
from .customspec import CustomSpec, host_arguments, instance_arguments, spec_applies
from .dependencies import SystemPlan, dependency_closure, load_graph, plan
from .discovery import DiscoveryError, discover_host, instance_status
from .models import (
    STATUS_UNKNOWN,
    Activity,
    ActivityLogEntry,
    Host,
    Instance,
    System,
    activity_systems,
)
from .roles import Role
from .status import refresh_databases

logger = logging.getLogger(__name__)

_LEVELS = {"error": "ERROR", "warning": "WARNING"}
_BUILT_IN_INSTANCE_OPERATIONS = ("START", "STOP", "RESTART")


class OperationKind(StrEnum):
    START = "START"
    STOP = "STOP"
    RESTART = "RESTART"
    DISCOVER = "DISCOVER"
    # A custom operation: a script configured in the host agent's operations.d.
    CUSTOM = "CUSTOM"


class TargetType(StrEnum):
    INSTANCE = "INSTANCE"
    SYSTEM = "SYSTEM"
    HOST = "HOST"
    # A mass operation: a parent activity whose children do the work.
    MASS = "MASS"


def _cancel_note(activity: Activity) -> str:
    if activity.kind in _BUILT_IN_INSTANCE_OPERATIONS:
        return " Starts and stops already requested on the hosts keep running."
    return ""


class ActivityStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # A mass operation in which some targets succeeded and others did not.
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"


MAX_PARALLEL = 20
MAX_MASS_TARGETS = 500


ACTIVE_STATUSES = (ActivityStatus.PENDING, ActivityStatus.RUNNING)

DEFAULT_TIMEOUT_SECONDS = 600


class OperationConflictError(Exception):
    """Another activity is still running for the same system."""


class TargetNotFoundError(Exception):
    """The instance or system does not exist."""


class NotAssignedError(Exception):
    """The instance is not assigned to a system and cannot be operated."""


class NotActiveError(Exception):
    """The activity has already finished."""


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
        # Activities a user asked to cancel -> user name; their tasks are being cancelled.
        self._cancel_requests: dict[int, str] = {}

    # --- submitting ---------------------------------------------------------------------

    async def submit_instance(
        self,
        session: AsyncSession,
        instance_id: int,
        kind: OperationKind,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        requested_by: str | None = None,
        *,
        parent_id: int | None = None,
    ) -> Activity:
        instance = await session.scalar(
            select(Instance).where(Instance.id == instance_id).options(selectinload(Instance.sid))
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
            requested_by,
            parent_id=parent_id,
        )

    async def submit_system(
        self,
        session: AsyncSession,
        system_id: int,
        kind: OperationKind,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        requested_by: str | None = None,
        *,
        parent_id: int | None = None,
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
            requested_by,
            parent_id=parent_id,
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
        requested_by: str | None = None,
        *,
        parent_id: int | None = None,
        host_id: int | None = None,
        options: str | None = None,
    ) -> Activity:
        # One activity per system at a time: operations on a system and on its instances
        # would otherwise work against each other. A system operation also covers the
        # systems it depends on. Host operations are exclusive per host.
        async with self._submit_lock:
            conflict = or_(
                activity_systems.c.system_id.in_(involved),
                Activity.system_id.in_(involved),
            )
            if host_id is not None:
                conflict = Activity.host_id == host_id
            active = await session.scalar(
                select(Activity)
                .outerjoin(activity_systems)
                .where(Activity.status.in_(ACTIVE_STATUSES), conflict)
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
                host_id=host_id,
                parent_id=parent_id,
                options=options,
                status=ActivityStatus.PENDING,
                requested_by=requested_by,
                timeout_seconds=timeout_seconds,
                requested_at=_now(),
            )
            activity.systems = list(
                await session.scalars(select(System).where(System.id.in_(involved)))
            )
            session.add(activity)
            await session.commit()
        self._start_task(activity.id)
        return activity

    def _start_task(self, activity_id: int) -> None:
        task = asyncio.create_task(self._run(activity_id), name=f"activity-{activity_id}")
        self._tasks[activity_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(activity_id, None))

    async def submit_host(
        self,
        session: AsyncSession,
        host_id: int,
        kind: OperationKind,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        requested_by: str | None = None,
        *,
        parent_id: int | None = None,
    ) -> Activity:
        """Run a host operation (so far only a discovery) as an activity."""
        host = await session.get(Host, host_id)
        if host is None:
            raise TargetNotFoundError("Host not found")
        if kind is not OperationKind.DISCOVER:
            raise ValueError(f"{kind} is not a host operation")
        return await self._submit(
            session,
            kind,
            TargetType.HOST,
            host.name,
            None,
            None,
            timeout_seconds,
            set(),
            requested_by,
            parent_id=parent_id,
            host_id=host.id,
        )

    async def submit_custom(
        self,
        session: AsyncSession,
        spec: CustomSpec,
        target_type: TargetType,
        target_id: int,
        parameters: dict[str, str],
        timeout_seconds: int | None = None,
        requested_by: str | None = None,
        *,
        parent_id: int | None = None,
    ) -> Activity:
        """Run a custom operation (a script in the host agent's operations.d) on a target.

        ``parameters`` are the checked values, PARAM-<name> -> value (customops.py).
        """
        if TargetType(spec.target) != target_type:
            raise ValueError(f"{spec.name} runs on {spec.target}, not on {target_type}")
        system_id: int | None = None
        instance_id: int | None = None
        host_id: int | None = None
        involved: set[int] = set()
        if target_type == TargetType.INSTANCE:
            instance = await session.scalar(
                select(Instance).where(Instance.id == target_id).options(selectinload(Instance.sid))
            )
            if instance is None:
                raise TargetNotFoundError("Instance not found")
            if instance.system_id is None:
                raise NotAssignedError(f"Instance {instance.label} is not assigned to a system")
            label, system_id, instance_id = instance.label, instance.system_id, instance.id
            involved = {instance.system_id}
        elif target_type == TargetType.SYSTEM:
            system = await session.get(System, target_id)
            if system is None:
                raise TargetNotFoundError("System not found")
            label, system_id, involved = system.name, system.id, {system.id}
        else:
            host = await session.get(Host, target_id)
            if host is None:
                raise TargetNotFoundError("Host not found")
            label, host_id = host.name, host.id
        options = json.dumps({"custom": spec.to_dict(), "parameters": parameters})
        return await self._submit(
            session,
            OperationKind.CUSTOM,
            target_type,
            label,
            system_id,
            instance_id,
            timeout_seconds or spec.timeout_seconds,
            involved,
            requested_by,
            parent_id=parent_id,
            host_id=host_id,
            options=options,
        )

    async def cancel(self, session: AsyncSession, activity_id: int, user: str) -> Activity:
        """Cancel a running activity on behalf of ``user``.

        Start, stop and restart stop waiting and request nothing more; what was already
        requested on the hosts keeps running. Custom operations are cancelled on the host
        agent (CancelOperation, O-HARD). A mass operation starts no further children and
        cancels the running ones.
        """
        activity = await session.get(Activity, activity_id)
        if activity is None:
            raise TargetNotFoundError("Activity not found")
        if activity.status not in ACTIVE_STATUSES:
            raise NotActiveError(f"Activity {activity_id} has already finished")
        self._cancel_requests[activity_id] = user
        task = self._tasks.get(activity_id)
        if task is not None:
            task.cancel()
            await asyncio.wait([task])
        else:
            self._cancel_requests.pop(activity_id, None)
            await self._finish_cancelled(session, activity, f"Cancelled by {user}.")
        session.expire_all()
        return await session.get(Activity, activity_id)

    async def submit_mass(
        self,
        session: AsyncSession,
        kind: OperationKind,
        target_type: TargetType,
        target_ids: list[int],
        *,
        parallel: int = 5,
        stop_on_error: bool = False,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        requested_by: str | None = None,
        custom: CustomSpec | None = None,
        parameters: dict[str, str] | None = None,
    ) -> Activity:
        """Run ``kind`` on many targets as one parent activity with a child per target.

        Up to ``parallel`` children run at the same time. Systems are ordered by their
        dependencies: for a start or restart a selected system waits for the selected
        systems it depends on, for a stop the dependents go first. A child that would
        overlap with a running sibling (e.g. a shared database system) waits for it. With
        ``stop_on_error`` no further children start after a failure.
        """
        ids = list(dict.fromkeys(target_ids))
        if not ids:
            raise TargetNotFoundError("No targets selected")
        if len(ids) > MAX_MASS_TARGETS:
            raise ValueError(f"At most {MAX_MASS_TARGETS} targets")
        if not 1 <= parallel <= MAX_PARALLEL:
            raise ValueError(f"parallel must be 1 to {MAX_PARALLEL}")
        model = {TargetType.SYSTEM: System, TargetType.INSTANCE: Instance, TargetType.HOST: Host}
        known = set(
            await session.scalars(
                select(model[target_type].id).where(model[target_type].id.in_(ids))
            )
        )
        if missing := [i for i in ids if i not in known]:
            raise TargetNotFoundError(f"Unknown targets: {missing}")
        options = {
            "target_type": target_type.value,
            "targets": ids,
            "parallel": parallel,
            "stop_on_error": stop_on_error,
        }
        if custom is not None:
            options |= {"custom": custom.to_dict(), "parameters": parameters or {}}
        noun = {"SYSTEM": "systems", "INSTANCE": "instances", "HOST": "hosts"}[target_type]
        activity = Activity(
            kind=kind,
            target_type=TargetType.MASS,
            target_label=(f"{custom.name}: " if custom else "") + f"{len(ids)} {noun}",
            status=ActivityStatus.PENDING,
            requested_by=requested_by,
            timeout_seconds=timeout_seconds,
            requested_at=_now(),
            options=json.dumps(options),
        )
        activity.systems = []
        session.add(activity)
        await session.commit()
        self._start_task(activity.id)
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
        try:
            await self._run_activity(activity_id)
        except asyncio.CancelledError:
            # The cancellation may have hit the session anywhere, even while it connected;
            # the activity is finished in a fresh session.
            user = self._cancel_requests.pop(activity_id, None)
            async with self._session_factory() as session:
                activity = await session.get(Activity, activity_id)
                if activity is not None and activity.status in ACTIVE_STATUSES:
                    if user is None:
                        await self._finish(session, activity, "Interrupted by server shutdown.")
                    else:
                        await self._finish_cancelled(
                            session, activity, f"Cancelled by {user}.{_cancel_note(activity)}"
                        )
            if user is None:
                raise

    async def _run_activity(self, activity_id: int) -> None:
        async with self._session_factory() as session:
            activity = await session.get(Activity, activity_id)
            if activity is None:
                return
            activity.status = ActivityStatus.RUNNING
            activity.started_at = _now()
            await session.commit()
            if activity.target_type == TargetType.MASS:
                await self._run_mass(session, activity)
                return
            error: str | None = None
            try:
                await self._execute(session, activity)
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
        if activity.target_type == TargetType.HOST:
            return  # a discovery has just read the databases
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
        await refresh_databases(session, hosts.values(), self._clients)
        await session.commit()

    # --- mass operations ----------------------------------------------------------------

    async def _run_mass(self, session: AsyncSession, parent: Activity) -> None:
        options = json.loads(parent.options or "{}")
        target_type = TargetType(options["target_type"])
        targets: list[int] = options["targets"]
        parallel: int = options["parallel"]
        stop_on_error: bool = options["stop_on_error"]
        kind = OperationKind(parent.kind)
        labels = await self._target_labels(session, target_type, targets)
        prerequisites = await self._prerequisites(session, kind, target_type, targets)
        await self._log(
            session,
            parent,
            f"{kind} on {parent.target_label}, {parallel} at a time, "
            + ("stop at the first failure" if stop_on_error else "continue after failures"),
        )

        pending = list(targets)
        running: dict[int, int] = {}  # child activity id -> target id
        results: dict[int, str] = {}  # target id -> status or SKIPPED
        try:
            while True:
                failed = any(r == ActivityStatus.FAILED for r in results.values())
                halted = stop_on_error and failed
                launched = False
                for target in list(pending):
                    if halted or len(running) >= parallel:
                        break
                    needed = prerequisites.get(target, set())
                    if any(results.get(n) not in (None, ActivityStatus.SUCCEEDED) for n in needed):
                        pending.remove(target)
                        results[target] = "SKIPPED"
                        await self._log(
                            session, parent, f"{labels[target]}: skipped, a dependency failed"
                        )
                        continue
                    if not needed <= results.keys():
                        continue  # waits for its dependencies
                    try:
                        child = await self._submit_child(session, kind, target_type, target, parent)
                    except OperationConflictError as exc:
                        if running:
                            continue  # probably blocked by a sibling; try again later
                        pending.remove(target)
                        results[target] = "SKIPPED"
                        await self._log(session, parent, f"{labels[target]}: skipped, {exc}")
                        continue
                    except (TargetNotFoundError, NotAssignedError) as exc:
                        pending.remove(target)
                        results[target] = ActivityStatus.FAILED
                        await self._log(session, parent, f"{labels[target]}: {exc}")
                        continue
                    pending.remove(target)
                    running[child.id] = target
                    launched = True
                    await self._log(
                        session, parent, f"{labels[target]}: activity {child.id} started"
                    )
                if not running:
                    if not launched:
                        break
                    continue
                await self._wait_for_any(list(running))
                for child_id in [c for c in running if c not in self._tasks]:
                    target = running.pop(child_id)
                    status = await session.scalar(
                        select(Activity.status).where(Activity.id == child_id)
                    )
                    results[target] = status or ActivityStatus.FAILED
                    await self._log(
                        session, parent, f"{labels[target]}: activity {child_id} {status}"
                    )
        except asyncio.CancelledError:
            await session.rollback()
            user = self._cancel_requests.pop(parent.id, None)
            if user is None:
                await self._finish(session, parent, "Interrupted by server shutdown.")
                raise
            tasks = []
            for child_id in running:
                self._cancel_requests[child_id] = user
                if (task := self._tasks.get(child_id)) is not None:
                    task.cancel()
                    tasks.append(task)
            if tasks:
                await asyncio.wait(tasks)
            for child_id, target in running.items():
                results[target] = (
                    await session.scalar(select(Activity.status).where(Activity.id == child_id))
                    or ActivityStatus.CANCELLED
                )
            for target in pending:
                results[target] = "SKIPPED"
            await self._log(session, parent, f"Cancelled by {user}")
            await self._finish_mass(session, parent, results, cancelled_by=user)
            return

        for target in pending:
            results[target] = "SKIPPED"
            await self._log(session, parent, f"{labels[target]}: not started")
        await self._finish_mass(session, parent, results)

    async def _submit_child(
        self,
        session: AsyncSession,
        kind: OperationKind,
        target_type: TargetType,
        target: int,
        parent: Activity,
    ) -> Activity:
        common = {"requested_by": parent.requested_by}
        timeout = parent.timeout_seconds
        if kind is OperationKind.CUSTOM:
            options = json.loads(parent.options or "{}")
            return await self.submit_custom(
                session,
                CustomSpec.from_dict(options["custom"]),
                target_type,
                target,
                options.get("parameters", {}),
                timeout,
                parent_id=parent.id,
                **common,
            )
        if target_type == TargetType.HOST:
            return await self.submit_host(
                session, target, kind, timeout, parent_id=parent.id, **common
            )
        if target_type == TargetType.INSTANCE:
            return await self.submit_instance(
                session, target, kind, timeout, parent_id=parent.id, **common
            )
        return await self.submit_system(
            session, target, kind, timeout, parent_id=parent.id, **common
        )

    async def _wait_for_any(self, activity_ids: list[int]) -> None:
        tasks = [self._tasks[i] for i in activity_ids if i in self._tasks]
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # A task leaves self._tasks in its done callback, which runs on the next loop pass.
        await asyncio.sleep(0)

    @staticmethod
    async def _target_labels(
        session: AsyncSession, target_type: TargetType, targets: list[int]
    ) -> dict[int, str]:
        if target_type == TargetType.SYSTEM:
            rows = await session.execute(
                select(System.id, System.name).where(System.id.in_(targets))
            )
            return {i: f"System {name}" for i, name in rows}
        if target_type == TargetType.HOST:
            rows = await session.execute(select(Host.id, Host.name).where(Host.id.in_(targets)))
            return {i: f"Host {name}" for i, name in rows}
        instances = await session.scalars(
            select(Instance).where(Instance.id.in_(targets)).options(selectinload(Instance.sid))
        )
        return {i.id: f"Instance {i.label}" for i in instances}

    @staticmethod
    async def _prerequisites(
        session: AsyncSession, kind: OperationKind, target_type: TargetType, targets: list[int]
    ) -> dict[int, set[int]]:
        """target -> selected targets that must have succeeded before it may begin."""
        if target_type != TargetType.SYSTEM or kind is OperationKind.CUSTOM:
            return {}
        graph = await load_graph(session)
        selected = set(targets)
        needs = {t: (dependency_closure(t, graph) - {t}) & selected for t in targets}
        if kind == OperationKind.STOP:
            # Dependents first: a system waits for the selected systems that need it.
            return {t: {o for o in targets if t in needs[o]} for t in targets}
        return needs

    async def _finish_mass(
        self,
        session: AsyncSession,
        parent: Activity,
        results: dict[int, str],
        cancelled_by: str | None = None,
    ) -> None:
        succeeded = sum(1 for r in results.values() if r == ActivityStatus.SUCCEEDED)
        failed = sum(1 for r in results.values() if r == ActivityStatus.FAILED)
        cancelled = sum(1 for r in results.values() if r == ActivityStatus.CANCELLED)
        skipped = len(results) - succeeded - failed - cancelled
        summary = f"{succeeded} succeeded, {failed} failed, {skipped} skipped"
        if cancelled:
            summary += f", {cancelled} cancelled"
        parent.finished_at = _now()
        if cancelled_by is not None:
            parent.status = ActivityStatus.CANCELLED
            parent.error = f"Cancelled by {cancelled_by}: {summary}"
            session.add(self._entry(parent, "WARNING", f"Finished: {summary}"))
        elif succeeded == len(results):
            parent.status = ActivityStatus.SUCCEEDED
            session.add(self._entry(parent, "INFO", f"Finished: {summary}"))
        else:
            parent.status = ActivityStatus.PARTIAL if succeeded else ActivityStatus.FAILED
            parent.error = summary
            session.add(self._entry(parent, "ERROR", f"Finished: {summary}"))
        await session.commit()

    async def _finish_cancelled(
        self, session: AsyncSession, activity: Activity, message: str
    ) -> None:
        activity.finished_at = _now()
        activity.status = ActivityStatus.CANCELLED
        activity.error = message
        session.add(self._entry(activity, "WARNING", message))
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
        if kind is OperationKind.CUSTOM:
            await self._execute_custom(session, activity)
            return
        if activity.target_type == TargetType.HOST:
            await self._discover(session, activity)
            return
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
                        select(System.id, System.name).where(System.id.in_(system_plan.involved))
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

    # --- custom operations --------------------------------------------------------------

    async def _execute_custom(self, session: AsyncSession, activity: Activity) -> None:
        """Run the script of a custom operation through the host agent and follow it.

        For a system the operation runs on each matching instance, in steps given by the
        ordering of the definition: all at once, by roles like a start, or one after another.
        """
        options = json.loads(activity.options or "{}")
        spec = CustomSpec.from_dict(options["custom"])
        values: dict[str, str] = options.get("parameters", {})
        steps: list[list[tuple[str, Host, dict[str, str]]]]
        if activity.target_type == TargetType.HOST:
            host = await session.get(Host, activity.host_id)
            if host is None:
                raise OperationFailedError("The host no longer exists")
            steps = [[("", host, host_arguments(host) | values)]]
        else:
            system = await session.scalar(
                select(System)
                .where(System.id == activity.system_id)
                .options(
                    selectinload(System.instances).selectinload(Instance.host),
                    selectinload(System.instances).selectinload(Instance.sid),
                )
            )
            if system is None:
                raise OperationFailedError("The system no longer exists")
            if activity.target_type == TargetType.INSTANCE:
                instances = [i for i in system.instances if i.id == activity.instance_id]
                if not instances:
                    raise OperationFailedError("The instance is no longer part of the system")
                groups = [instances]
            else:
                instances = [i for i in system.instances if spec_applies(spec, i, system=True)]
                if not instances:
                    raise OperationFailedError(
                        f"System {system.name} has no instances this operation applies to"
                    )
                groups = self._custom_steps(spec.ordering, instances)
            several = activity.target_type == TargetType.SYSTEM
            steps = [
                [
                    (i.label if several else "", i.host, instance_arguments(i, system) | values)
                    for i in group
                ]
                for group in groups
            ]

        await self._log(
            session,
            activity,
            f"{spec.name}: host agent operation {spec.agent_operation}, parameters "
            + (", ".join(f"{k}={v}" for k, v in values.items()) or "none"),
        )
        deadline = self._clock() + activity.timeout_seconds
        lock = asyncio.Lock()
        for step in steps:
            outcomes = await asyncio.gather(
                *(
                    self._run_host_operation(
                        session, activity, spec, label, host, arguments, deadline, lock
                    )
                    for label, host, arguments in step
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
                    raise outcome
            errors = [str(o) for o in outcomes if isinstance(o, Exception)]
            if errors:
                raise OperationFailedError("; ".join(errors))

    def _custom_steps(self, ordering: str, instances: list[Instance]) -> list[list[Instance]]:
        if ordering == "sequential":
            return [[i] for i in sorted(instances, key=lambda i: (i.priority, i.label))]
        if ordering == "roles":
            steps = []
            for tracks in self._phases(instances, start=True):
                steps.append([i for track in tracks for group in track for i in group])
            return steps
        return [instances]

    async def _run_host_operation(
        self,
        session: AsyncSession,
        activity: Activity,
        spec: CustomSpec,
        label: str,
        host: Host,
        arguments: dict[str, str],
        deadline: float,
        lock: asyncio.Lock,
    ) -> None:
        """Start the script on one host, copy its messages into the log until it ends.

        Seen on a real host agent: GetOperationResults answers a running operation with fault
        255 and the messages so far, a failed one (exit code other than 0) with fault 127.
        """
        prefix = f"{label}: " if label else ""
        logged = 0

        async def log(level: str, message: str) -> None:
            async with lock:
                session.add(self._entry(activity, level, prefix + message))
                await session.commit()

        async def log_new(entries: list[tuple[str, str]]) -> None:
            nonlocal logged
            # "Executing ... hook" only appears while the script runs.
            lasting = [e for e in entries if "saphostcontrol: Executing" not in e[1]]
            for key, value in lasting[logged:]:
                await log(_LEVELS.get(key.lower(), "INFO"), value)
            logged = max(logged, len(lasting))

        try:
            async with self._clients.hostagent(host) as agent:
                started = await agent.execute_async(spec.agent_operation, arguments)
        except SoapFaultError as exc:
            raise OperationFailedError(
                f"{prefix}ExecuteOperation {spec.agent_operation} on {host.name} was rejected: "
                f"{exc.faultstring}. Is the operation configured in operations.d of this host?"
            ) from exc
        except SoapConnectionError as exc:
            raise OperationFailedError(
                f"{prefix}the host agent of {host.name} could not be reached ({exc})"
            ) from exc
        operation_id = started.operation_id
        if not operation_id:
            raise OperationFailedError(f"{prefix}the host agent returned no operation ID")
        await log("INFO", f"Started on {host.name}, operation {operation_id}")

        try:
            while True:
                async with self._clients.hostagent(host) as agent:
                    finished = await agent.is_operation_finished(operation_id)
                    try:
                        result = await agent.get_operation_results(operation_id)
                    except OperationFault as fault:
                        await log_new(fault.result.entries)
                        if fault.code == FAULT_TIMEOUT and not finished:
                            pass  # still running
                        elif fault.code == FAULT_TIMEOUT:
                            continue  # finished in between; read the results again
                        else:
                            errors = [v for k, v in fault.result.entries if k.lower() == "error"]
                            reason = errors[-1] if errors else fault.faultstring
                            raise OperationFailedError(f"{prefix}{reason}") from fault
                    else:
                        await log_new(result.entries)
                        if finished:
                            return
                if self._deadline_reached(deadline):
                    await self._cancel_host_operation(host, operation_id, log, log_new)
                    raise OperationFailedError(
                        f"{prefix}Timeout after {activity.timeout_seconds} s; the operation "
                        "was cancelled on the host"
                    )
                await self._sleep(self._poll_interval)
        except asyncio.CancelledError:
            if activity.id in self._cancel_requests:
                await self._cancel_host_operation(host, operation_id, log, log_new)
            else:
                await log(
                    "WARNING",
                    f"Interrupted; operation {operation_id} may still run on {host.name}",
                )
            raise

    async def _cancel_host_operation(self, host: Host, operation_id: str, log, log_new) -> None:
        try:
            async with self._clients.hostagent(host) as agent:
                result = await agent.cancel_operation(operation_id)
        except (SoapError, SecretError) as exc:
            await log("ERROR", f"CancelOperation for {operation_id} failed: {exc}")
            return
        await log_new(result.entries)
        await log("WARNING", f"Operation {operation_id} cancelled on {host.name}")

    async def _discover(self, session: AsyncSession, activity: Activity) -> None:
        host = await session.scalar(
            select(Host)
            .where(Host.id == activity.host_id)
            .options(selectinload(Host.instances), selectinload(Host.databases))
        )
        if host is None:
            raise OperationFailedError("The host no longer exists")
        try:
            result = await discover_host(session, host, self._clients)
        except DiscoveryError as exc:
            await session.commit()  # keep the "unreachable" marker on the host
            raise OperationFailedError(f"The host agent could not be queried: {exc}") from exc
        await session.commit()
        await self._log(
            session,
            activity,
            f"Discovery: {len(result.instances_added)} instances new, "
            f"{len(result.instances_updated)} updated, {len(result.instances_missing)} missing; "
            f"{len(result.databases_added)} databases new, "
            f"{len(result.databases_updated)} updated, {len(result.databases_missing)} missing",
        )
        for warning in result.warnings:
            session.add(self._entry(activity, "WARNING", warning))
        await session.commit()

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
                        f"Timeout after {timeout} s: system {name} is {last}, expected all {target}"
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
