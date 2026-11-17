"""Texts of the web interface in English and German."""

from __future__ import annotations

LANGUAGES = ("en", "de")
DEFAULT_LANGUAGE = "en"

# key -> (English, German)
TEXTS: dict[str, tuple[str, str]] = {
    "app.title": ("Landscape Orchestrator", "Landscape Orchestrator"),
    "nav.overview": ("Overview", "Übersicht"),
    "nav.landscape": ("Landscape", "Landscape"),
    "nav.activities": ("Activities", "Activities"),
    "nav.configuration": ("Configuration", "Configuration"),
    "nav.administration": ("Administration", "Administration"),
    "tab.systems": ("Systems", "Systeme"),
    "tab.hosts": ("Hosts", "Hosts"),
    "tab.activities": ("Activities", "Aktivitäten"),
    "tab.logs": ("Logs", "Protokolle"),
    "tab.pools": ("Pools", "Pools"),
    "action.configure": ("Configure", "Konfigurieren"),
    "action.view": ("Show in landscape", "In der Landscape anzeigen"),
    "action.save": ("Save", "Speichern"),
    "overview.instances": ("Instances", "Instanzen"),
    "overview.hosts": ("Hosts", "Hosts"),
    "overview.activities_24h": ("Activities (24 h)", "Aktivitäten (24 h)"),
    "overview.none": ("Nothing yet.", "Noch nichts vorhanden."),
    "reach.GREEN": ("reachable", "erreichbar"),
    "reach.RED": ("not reachable", "nicht erreichbar"),
    "reach.UNKNOWN": ("not checked yet", "noch nicht geprüft"),
    "field.pool": ("Pool", "Pool"),
    "field.description": ("Description", "Beschreibung"),
    "pool.all": ("All", "Alle"),
    "pool.none": ("No pool", "Kein Pool"),
    "pool.add": ("Add pool", "Pool anlegen"),
    "pool.delete": ("Delete", "Löschen"),
    "pool.delete_confirm": (
        "Delete this pool? Its systems stay and are no longer in a pool.",
        "Diesen Pool löschen? Seine Systeme bleiben erhalten und gehören dann zu keinem Pool.",
    ),
    "pool.empty": ("No pools yet.", "Noch keine Pools."),
    "pool.none_defined": ("No pools are defined yet.", "Es sind noch keine Pools angelegt."),
    "pool.explanation": (
        "A pool groups systems that belong together, for example the development, test and "
        "production system of one product. A system belongs to at most one pool.",
        "Ein Pool fasst Systeme zusammen, die zusammengehören, zum Beispiel Entwicklungs-, "
        "Test- und Produktivsystem eines Produkts. Ein System gehört zu höchstens einem Pool.",
    ),
    "config.systems_hint": (
        "Systems are created by the discovery of their hosts.",
        "Systeme entstehen durch die Discovery ihrer Hosts.",
    ),
    "config.no_systems": (
        "No systems yet. Add a host and run a discovery.",
        "Noch keine Systeme. Lege einen Host an und starte eine Discovery.",
    ),
    "credentials.host_used": ("those of the host", "die des Hosts"),
    "logs.hint": (
        "The latest {limit} log entries of all activities.",
        "Die letzten {limit} Protokolleinträge aller Aktivitäten.",
    ),
    "logs.empty": ("No log entries yet.", "Noch keine Protokolleinträge."),
    "admin.planned": (
        "Platform settings will follow. Current configuration:",
        "Plattform-Einstellungen folgen. Aktuelle Konfiguration:",
    ),
    "admin.version": ("Version", "Version"),
    "admin.database": ("Database", "Datenbank"),
    "admin.session": ("Sign-in valid for", "Anmeldung gültig für"),
    "admin.session_hours": ("{hours} hours", "{hours} Stunden"),
    "notice.pool_created": ("Pool created.", "Pool angelegt."),
    "notice.pool_deleted": ("Pool deleted.", "Pool gelöscht."),
    "notice.pool_saved": ("Pool saved.", "Pool gespeichert."),
    "error.pool_exists": (
        "A pool with this name already exists.",
        "Ein Pool mit diesem Namen existiert bereits.",
    ),
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
        "Optional. Starts and stops run through the host agent with the credentials of the "
        "host, and sapstartsrv is only read. Store credentials here, typically <sid>adm, only "
        "if the sapstartsrv of this system rejects reading with those of the host.",
        "Optional. Start und Stopp laufen über den Host Agent mit den Zugangsdaten des Hosts, "
        "den sapstartsrv liest das Tool nur. Hinterlege hier Zugangsdaten, typischerweise "
        "<sid>adm, nur wenn der sapstartsrv dieses Systems das Lesen mit denen des Hosts "
        "ablehnt.",
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
    "deps.title": ("Dependencies", "Abhängigkeiten"),
    "deps.depends_on": ("Depends on", "Hängt ab von"),
    "deps.required_by": ("Required by", "Wird benötigt von"),
    "deps.none": ("none", "keine"),
    "deps.add": ("Add dependency", "Abhängigkeit hinzufügen"),
    "deps.remove": ("Remove", "Entfernen"),
    "deps.no_candidates": (
        "No other system can be added.",
        "Es kann kein weiteres System hinzugefügt werden.",
    ),
    "deps.explanation": (
        "Starting this system starts the systems it depends on first, for example its "
        "database system. Stopping it stops this system first and then its dependencies, "
        "unless another system still needs them.",
        "Beim Starten dieses Systems werden zuerst die Systeme gestartet, von denen es "
        "abhängt, zum Beispiel sein Datenbanksystem. Beim Stoppen wird erst dieses System "
        "gestoppt, dann seine Abhängigkeiten, sofern kein anderes System sie noch benötigt.",
    ),
    "deps.required_note": (
        "Stopping this system does not stop the systems that need it. Stop those first.",
        "Das Stoppen dieses Systems stoppt nicht die Systeme, die es benötigen. "
        "Stoppe diese zuerst.",
    ),
    "notice.dependencies_saved": ("Dependencies saved.", "Abhängigkeiten gespeichert."),
    "error.dependency_self": (
        "A system cannot depend on itself.",
        "Ein System kann nicht von sich selbst abhängen.",
    ),
    "error.dependency_unknown": ("Unknown system.", "Unbekanntes System."),
    "error.dependency_cycle": (
        "This dependency would create a cycle.",
        "Diese Abhängigkeit würde einen Kreis bilden.",
    ),
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
