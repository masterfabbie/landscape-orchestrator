"""Texts of the web interface in English and German."""

from __future__ import annotations

LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"

# key -> (English, German)
TEXTS: dict[str, tuple[str, str]] = {
    "app.title": ("Landscape Orchestrator", "Landscape Orchestrator"),
    "nav.overview": ("Overview", "Übersicht"),
    "nav.hosts": ("Hosts", "Hosts"),
    "nav.activities": ("Activities", "Aktivitäten"),
    "nav.logout": ("Sign out", "Abmelden"),
    "login.title": ("Sign in", "Anmelden"),
    "login.token": ("API token", "API-Token"),
    "login.submit": ("Sign in", "Anmelden"),
    "login.hint": (
        "Enter the token configured in LO_API_TOKEN.",
        "Gib das Token ein, das in LO_API_TOKEN konfiguriert ist.",
    ),
    "login.invalid": ("The token is not valid.", "Das Token ist ungültig."),
    "overview.systems": ("Systems", "Systeme"),
    "overview.databases": ("Databases", "Datenbanken"),
    "overview.empty": (
        "No systems yet. Add a host and run a discovery.",
        "Noch keine Systeme. Lege einen Host an und starte eine Discovery.",
    ),
    "overview.status_note": (
        "Status as of the last discovery or operation.",
        "Status zum Zeitpunkt der letzten Discovery oder Operation.",
    ),
    "overview.running": ("Activity running", "Aktivität läuft"),
    "hosts.add": ("Add host", "Host anlegen"),
    "hosts.empty": ("No hosts yet.", "Noch keine Hosts."),
    "hosts.never": ("never", "nie"),
    "host.discover": ("Run discovery", "Discovery starten"),
    "host.delete": ("Delete host", "Host löschen"),
    "host.delete_confirm": (
        "Delete this host with its instances and databases?",
        "Diesen Host samt Instanzen und Datenbanken löschen?",
    ),
    "host.connection": ("Connection", "Verbindung"),
    "host.system_info": ("System information", "Systeminformationen"),
    "host.no_instances": (
        "No instances known. Run a discovery.",
        "Keine Instanzen bekannt. Starte eine Discovery.",
    ),
    "host.no_databases": ("No databases known.", "Keine Datenbanken bekannt."),
    "credentials.title": ("Credentials", "Zugangsdaten"),
    "credentials.save": ("Save credentials", "Zugangsdaten speichern"),
    "credentials.set": ("stored", "hinterlegt"),
    "credentials.missing": ("not set", "nicht gesetzt"),
    "credentials.host_hint": (
        "Used for the host agent, typically sapadm.",
        "Gilt für den Host Agent, typischerweise sapadm.",
    ),
    "credentials.system_hint": (
        "Used for the sapstartsrv of this system, typically <sid>adm. "
        "Without them the credentials of the host apply.",
        "Gilt für den sapstartsrv dieses Systems, typischerweise <sid>adm. "
        "Ohne sie gelten die Zugangsdaten des Hosts.",
    ),
    "field.name": ("Name", "Name"),
    "field.address": ("Address", "Adresse"),
    "field.address_hint": (
        "Hostname or IP; defaults to the name",
        "Hostname oder IP; Standard ist der Name",
    ),
    "field.hostagent_port": ("Host agent port", "Host-Agent-Port"),
    "field.port_offset": ("sapstartsrv port offset", "Port-Verschiebung sapstartsrv"),
    "field.port_offset_hint": ("only for test setups", "nur für Testaufbauten"),
    "field.https": ("Use HTTPS", "HTTPS verwenden"),
    "field.verify_tls": ("Verify certificate", "Zertifikat prüfen"),
    "field.username": ("User", "Benutzer"),
    "field.password": ("Password", "Passwort"),
    "field.os": ("Operating system", "Betriebssystem"),
    "field.cpus": ("CPUs", "CPUs"),
    "field.memory": ("Memory", "Arbeitsspeicher"),
    "field.reachable": ("Reachable", "Erreichbar"),
    "field.last_discovery": ("Last discovery", "Letzte Discovery"),
    "field.last_error": ("Last error", "Letzter Fehler"),
    "field.instance": ("Instance", "Instanz"),
    "field.instances": ("Instances", "Instanzen"),
    "field.host": ("Host", "Host"),
    "field.hostname": ("Hostname", "Hostname"),
    "field.type": ("Type", "Typ"),
    "field.status": ("Status", "Status"),
    "field.checked": ("Checked", "Geprüft"),
    "field.actions": ("Actions", "Aktionen"),
    "field.database": ("Database", "Datenbank"),
    "field.release": ("Release", "Release"),
    "field.system": ("System", "System"),
    "field.target": ("Target", "Ziel"),
    "field.operation": ("Operation", "Operation"),
    "field.requested": ("Requested", "Angefordert"),
    "field.finished": ("Finished", "Beendet"),
    "field.time": ("Time", "Zeit"),
    "field.message": ("Message", "Meldung"),
    "field.priority": ("Start priority", "Startpriorität"),
    "yes": ("yes", "ja"),
    "no": ("no", "nein"),
    "unknown": ("unknown", "unbekannt"),
    "op.start": ("Start", "Starten"),
    "op.stop": ("Stop", "Stoppen"),
    "op.restart": ("Restart", "Neu starten"),
    "op.start_system": ("Start system", "System starten"),
    "op.stop_system": ("Stop system", "System stoppen"),
    "op.restart_system": ("Restart system", "System neu starten"),
    "op.confirm": ("Run this operation?", "Diese Operation ausführen?"),
    "kind.START": ("Start", "Start"),
    "kind.STOP": ("Stop", "Stopp"),
    "kind.RESTART": ("Restart", "Neustart"),
    "target.INSTANCE": ("Instance", "Instanz"),
    "target.SYSTEM": ("System", "System"),
    "status.GREEN": ("running", "läuft"),
    "status.YELLOW": ("in transition", "im Übergang"),
    "status.RED": ("error", "Fehler"),
    "status.GRAY": ("stopped", "gestoppt"),
    "status.UNKNOWN": ("unknown", "unbekannt"),
    "status.RUNNING": ("running", "läuft"),
    "status.STOPPED": ("stopped", "gestoppt"),
    "status.STARTING": ("starting", "startet"),
    "status.STOPPING": ("stopping", "stoppt"),
    "status.WARNING": ("warning", "Warnung"),
    "status.INSERVICE": ("in service", "in Wartung"),
    "status.ERROR": ("error", "Fehler"),
    "activity.PENDING": ("pending", "wartet"),
    "activity.RUNNING": ("running", "läuft"),
    "activity.SUCCEEDED": ("succeeded", "erfolgreich"),
    "activity.FAILED": ("failed", "fehlgeschlagen"),
    "activities.empty": ("No activities yet.", "Noch keine Aktivitäten."),
    "activity.title": ("Activity", "Aktivität"),
    "activity.log": ("Log", "Protokoll"),
    "activity.refreshing": (
        "This page refreshes automatically while the activity is running.",
        "Diese Seite aktualisiert sich automatisch, solange die Aktivität läuft.",
    ),
    "activity.log_language": (
        "Log messages are recorded in English.",
        "Protokollmeldungen werden auf Englisch aufgezeichnet.",
    ),
    "itype.hana": ("SAP HANA", "SAP HANA"),
    "itype.scs": ("Central services", "Zentrale Dienste"),
    "itype.ers": ("Enqueue replication", "Enqueue-Replikation"),
    "itype.abap": ("ABAP application server", "ABAP-Anwendungsserver"),
    "itype.java": ("Java application server", "Java-Anwendungsserver"),
    "itype.dual": ("ABAP+Java application server", "ABAP+Java-Anwendungsserver"),
    "notice.host_created": ("Host created.", "Host angelegt."),
    "notice.host_deleted": ("Host deleted.", "Host gelöscht."),
    "notice.credentials_saved": ("Credentials saved.", "Zugangsdaten gespeichert."),
    "notice.discovered": (
        "Discovery finished: {added} new, {updated} updated, {missing} missing.",
        "Discovery abgeschlossen: {added} neu, {updated} aktualisiert, {missing} fehlend.",
    ),
    "error.host_exists": (
        "A host with this name already exists.",
        "Ein Host mit diesem Namen existiert bereits.",
    ),
    "error.credentials_incomplete": (
        "User and password must be given together.",
        "Benutzer und Passwort müssen zusammen angegeben werden.",
    ),
    "error.invalid_input": ("The input is not valid.", "Die Eingabe ist ungültig."),
    "error.discovery_failed": (
        "The host agent could not be queried: {detail}",
        "Der Host Agent konnte nicht abgefragt werden: {detail}",
    ),
    "error.conflict": (
        "Another activity is still running for this system.",
        "Für dieses System läuft bereits eine Aktivität.",
    ),
    "warning.discovery": ("Warnings", "Warnungen"),
}


def translate(language: str, key: str, **values: object) -> str:
    """Text for ``key``; unknown keys are returned unchanged so they stay visible."""
    texts = TEXTS.get(key)
    if texts is None:
        return key
    text = texts[1] if language == "de" else texts[0]
    return text.format(**values) if values else text


def pick_language(cookie: str | None, accept_language: str | None) -> str:
    if cookie in LANGUAGES:
        return cookie
    if accept_language:
        first = accept_language.split(",")[0].strip().lower()
        if first.startswith("de"):
            return "de"
    return DEFAULT_LANGUAGE


def instance_type_key(features: str | None) -> str | None:
    """Translation key of the instance type for a sapstartsrv feature list."""
    names = set((features or "").split("|"))
    if "HDB" in names:
        return "itype.hana"
    if "MESSAGESERVER" in names or "ENQUE" in names:
        return "itype.scs"
    if "ENQREP" in names:
        return "itype.ers"
    if "ABAP" in names and "J2EE" in names:
        return "itype.dual"
    if "ABAP" in names:
        return "itype.abap"
    if "J2EE" in names:
        return "itype.java"
    return None
