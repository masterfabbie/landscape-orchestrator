"""Filters of the landscape lists.

Within one filter one matching value is enough (pool A or B); all filters given must match.
Filter values come from the query string, so a filtered view can be bookmarked. Which
filters are shown is a per-browser choice (see the web layer).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .models import STATUS_UNKNOWN, Host, Instance, System
from .tags import matches, parse_tag, tag_texts

STATUS_VALUES = ("GREEN", "YELLOW", "RED", "GRAY", STATUS_UNKNOWN)
REACH_VALUES = ("GREEN", "RED", STATUS_UNKNOWN)
TYPE_VALUES = ("hana", "scs", "ers", "abap", "java", "dual", "agent")


@dataclass(frozen=True)
class FilterField:
    name: str
    multi: bool


SYSTEM_FILTERS = (
    FilterField("name", False),
    FilterField("status", True),
    FilterField("pool", True),
    FilterField("host", False),
    FilterField("type", True),
    FilterField("tag", True),
    FilterField("hosttag", True),
)
HOST_FILTERS = (
    FilterField("name", False),
    FilterField("reach", True),
    FilterField("os", False),
    FilterField("agent", True),
    FilterField("tag", True),
)
DEFAULT_VISIBLE = {
    "systems": ("name", "status", "pool", "tag"),
    "hosts": ("name", "reach", "tag"),
}
FIELDS = {"systems": SYSTEM_FILTERS, "hosts": HOST_FILTERS}
PARAMS = frozenset(f.name for fields in FIELDS.values() for f in fields)


def read_values(tab: str, query: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Filter values from the query string; empty values are dropped."""
    values: dict[str, list[str]] = {}
    for field in FIELDS[tab]:
        given = [v.strip() for v in query.get(field.name, ()) if v.strip()]
        if not field.multi:
            given = given[:1]
        if given:
            values[field.name] = given
    return values


def visible_fields(tab: str, cookie: str | None, values: Mapping[str, list[str]]) -> list[str]:
    """Filters to show: the saved choice, plus every filter that currently has a value."""
    known = [f.name for f in FIELDS[tab]]
    chosen = DEFAULT_VISIBLE[tab] if cookie is None else cookie.split(".")
    return [name for name in known if name in chosen or name in values]


def _contains(text: str | None, needle: str) -> bool:
    return needle.lower() in (text or "").lower()


def host_reach(host: Host) -> str:
    if host.reachable is None:
        return STATUS_UNKNOWN
    return "GREEN" if host.reachable else "RED"


def filter_systems(
    systems: Sequence[System],
    values: Mapping[str, list[str]],
    status: Callable[[System], str],
    type_of: Callable[[Instance], str | None],
) -> list[System]:
    """``status`` gives the overall status of a system, ``type_of`` the type of an instance
    (one of TYPE_VALUES or None)."""

    def keep(system: System) -> bool:
        if (name := values.get("name")) and not _contains(system.name, name[0]):
            return False
        if (wanted := values.get("status")) and status(system) not in wanted:
            return False
        if wanted := values.get("pool"):
            pool = str(system.pool_id) if system.pool_id else "none"
            if pool not in wanted:
                return False
        if host := values.get("host"):
            if not any(
                _contains(i.host.name, host[0]) or _contains(i.hostname, host[0])
                for i in system.instances
            ):
                return False
        if wanted := values.get("type"):
            if not any(type_of(i) in wanted for i in system.instances):
                return False
        if (wanted := values.get("tag")) and not matches(system.tags, wanted):
            return False
        if wanted := values.get("hosttag"):
            if not any(matches(i.host.tags, wanted) for i in system.instances):
                return False
        return True

    return [system for system in systems if keep(system)]


def filter_hosts(hosts: Sequence[Host], values: Mapping[str, list[str]]) -> list[Host]:
    def keep(host: Host) -> bool:
        if (name := values.get("name")) and not (
            _contains(host.name, name[0]) or _contains(host.address, name[0])
        ):
            return False
        if (wanted := values.get("reach")) and host_reach(host) not in wanted:
            return False
        if (os := values.get("os")) and not _contains(
            f"{host.os_name or ''} {host.os_version or ''}", os[0]
        ):
            return False
        if (wanted := values.get("agent")) and (host.hostagent_version or "") not in wanted:
            return False
        return not ((wanted := values.get("tag")) and not matches(host.tags, wanted))

    return [host for host in hosts if keep(host)]


def tag_options(owners: Sequence[Host | System]) -> list[str]:
    """Choices of a tag filter: every tag text in use, plus each key that has values, so
    "sla" can select all values of "sla"."""
    texts: set[str] = set()
    for owner in owners:
        for text in tag_texts(owner.tags):
            texts.add(text)
            texts.add(parse_tag(text)[0])
    return sorted(texts)
