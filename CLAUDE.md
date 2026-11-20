# Landscape Orchestrator – Hinweise für Claude

Ein Werkzeug zur zentralen Verwaltung von SAP-Systemlandschaften, angelehnt an den
Funktionsumfang von SAP Landscape Management. Es ist ein unabhängiges Projekt. Verwende im
Code und in der Oberfläche keine SAP-Produktnamen als Namen dieses Werkzeugs.

Das README beschreibt Bedienung, API und Einschränkungen. Diese Datei beschreibt, wie hier
gearbeitet wird und was offen ist.

## Arbeitsweise

- **Änderungen am Code erst vorschlagen und freigeben lassen.** Der Eigentümer möchte keine
  Codeänderungen ohne Zustimmung.
- **Keine echten Daten ins Repository:** keine echten Hostnamen, IP-Adressen, SIDs realer
  Systeme, Passwörter, Tokens oder rohen Antworten echter Hosts. Beispiele und Tests nutzen
  die Namen der simulierten Landschaft (`sapapp1`, `sapdb1`, System `DEV`).
- **Annahmen kennzeichnen.** Was nicht aus einer WSDL oder einer echten Antwort belegt ist,
  wird im Code als Annahme kommentiert und im README unter den Einschränkungen genannt.
- Code, Kommentare und Protokollmeldungen sind englisch. README und diese Datei sind deutsch.
  Die Oberfläche ist zweisprachig; jeder Text braucht beide Sprachen
  (`src/landscape_orchestrator/web/i18n.py`, ein Test prüft das).

## Befehle

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check .
```

Server gegen die simulierte Landschaft, braucht `LO_SECRET_KEY` (erzeugen mit
`.venv/bin/lo-keygen`). Ohne Benutzer gibt er einen Einrichtungscode für `/setup` aus:

```bash
.venv/bin/lo-server --mock
```

Benutzer ohne Oberfläche anlegen (fragt das Passwort ab):

```bash
.venv/bin/lo-user add chief --role admin
```

Schema der Datenbank aus `LO_DATABASE_URL` anzeigen bzw. migrieren (macht der Server beim Start
auch selbst):

```bash
.venv/bin/lo-db current
```

Vor jedem Commit müssen Tests und Lint grün sein. Die CI prüft beides auf Python 3.11 und 3.13.

## Aufbau

Alles liegt unter `src/landscape_orchestrator/`.

| Modul | Aufgabe |
|---|---|
| `soapxml.py` | SOAP-Envelopes bauen und lesen, gemeinsam für Clients und Mock |
| `clients/` | asynchrone SOAP-Clients: `HostAgentClient` (SAPHostControl, Port 1128/1129), `SapControlClient` (sapstartsrv, Port 5NN13/5NN14), gemeinsame Fehlerklassen in `base.py` |
| `models.py`, `db.py` | Datenmodell (SQLAlchemy, asynchron): Host, Sid, Instance, System, Pool, Database, Activity, ActivityLogEntry; Migration beim Start, Sicherungskopie, Befehl `lo-db` |
| `migrations/` | Alembic-Migrationen (`env.py`, `versions/`) |
| `hosts.py` | Verbindungsdaten eines Hosts ändern |
| `users.py` | Benutzer, Rollen, Passwörter (scrypt), Sperre nach Fehlversuchen, Sitzungs-Cookies, persönliche API-Tokens, Einstellungen pro Benutzer |
| `permissions.py` | welche Rolle eine Route braucht, gemeinsam für API und Oberfläche |
| `usercli.py` | Befehl `lo-user` |
| `crypto.py`, `settings.py` | Verschlüsselung gespeicherter Passwörter (Fernet), Konfiguration aus Umgebungsvariablen |
| `connections.py` | baut Clients für Inventareinträge samt Zugangsdaten |
| `discovery.py` | liest einen Host aus und aktualisiert das Inventar |
| `dependencies.py` | Abhängigkeiten zwischen Systemen, Reihenfolge von Start und Stopp |
| `roles.py` | Rolle einer Instanz aus ihren Features: Datenbank, zentrale Dienste, Anwendungsserver, Diagnostics Agent; Instanztyp für Filter (`instance_type_key`) |
| `tags.py` | Tags von Hosts und Systemen (Schlüssel mit optionalem Wert): prüfen, ersetzen, vergleichen |
| `filters.py` | Filter der Landscape-Listen (Werte aus der Adresse, sichtbare Filter) |
| `status.py` | Status lesen ohne Aktivität: Instanzen eines Systems (Knopf „Status aktualisieren“) und Datenbanken |
| `systems.py` | Systeme als Gruppen von Instanzen: anlegen, zuordnen, entfernen, löschen, Namens- und Zuordnungsvorschläge, abgeleitete „Hängt ab von“-SIDs |
| `pools.py` | Zuordnung von Systemen zu Pools |
| `operations.py` | Start, Stopp, Neustart, Discovery als Hintergrund-Aktivitäten, Massenoperationen (`OperationRunner`) |
| `catalog.py` | Katalog der Operationen: eingebaute und eigene (`Catalog`, je App eine Instanz, neu geladen nach Änderungen), Ziele, nötige Rolle, als Massenoperation erlaubt |
| `customspec.py` | Beschreibung einer eigenen Operation (`CustomSpec`, als JSON in der Aktivität), Kontextparameter, `SERVICE_SUBTYPE` |
| `customops.py` | eigene Operationen und Parameter anlegen und prüfen, Parameterwerte prüfen (`parameter_values`) |
| `api/` | REST-API unter `/api`, geschützt durch persönliche API-Tokens; `create_app` baut die Anwendung |
| `web/` | servergerenderte Oberfläche (Jinja2), Anmeldung, Einrichtung, Konto, Übersetzungen |
| `mock/` | simulierter Host Agent und sapstartsrv für Entwicklung und Tests |

Wichtige Entwurfsentscheidungen:

- **Alle Starts und Stopps laufen über den Host Agent** (`StartInstance`, `StopInstance`) und
  brauchen nur `sapadm`. Ganze Systeme orchestriert das Tool selbst nach Rollen
  (`roles.py`): Datenbank und zentrale Dienste starten parallel, Anwendungsserver erst danach;
  Stopp umgekehrt. Innerhalb einer Rolle ordnet die Startpriorität. Diagnostics Agents
  (`SMDAGENT`) gehören zum System, sind aber nicht Teil von Systemoperationen und zählen nicht
  zum Systemstatus; einzeln bleiben sie bedienbar. Wunsch des Eigentümers, angelehnt an LaMa.
  `StartSystem` und `StopSystem` des sapstartsrv werden nicht genutzt, weil echte Systeme sie
  für `sapadm` ablehnen. Den sapstartsrv liest das Tool
  nur (`GetProcessList`, `GetSystemInstanceList`).
- **Systeme sind selbst angelegte Gruppen von Instanzen** mit frei wählbarem Namen; die
  Instanzen dürfen verschiedene SIDs haben (z. B. ABAP-System samt HANA-Instanz mit eigener
  SID). Die Discovery legt nur SIDs (Tabelle `sids`) und nicht zugeordnete Instanzen an
  (`instances.system_id` ist dann NULL), nie Systeme. Nur Instanzen eines Systems lassen sich
  bedienen und erscheinen in der Landscape. Vorschläge (Name aus der SID der
  Anwendungsinstanzen, Zuordnung zu dem einen System, das die SID schon enthält) werden nie
  automatisch übernommen. „Hängt ab von“ je Instanz wird aus den Rollen abgeleitet (nur
  Anwendungsserver hängen ab, von Datenbank und zentralen Diensten) und nur angezeigt.
- **Bezeichnung einer Instanz** ist `SID/NR`; gibt es SID/NR auf mehreren Hosts, kommt der
  Instanz-Hostname dazu (`Instance.label`, Zählung per `column_property`).
- **Version des Host Agents** kommt aus `ExecuteOperation` `versioninfo` (synchron,
  Timeout -1) und steht in der eigenen Tabelle `host_agent_info`. Geparst werden nur die
  Texte der Meldungen, nicht ihre Schlüssel.
- **Zugangsdaten** gibt es auf zwei Ebenen: Die des Hosts gelten für den Host Agent, die der
  SID für den sapstartsrv. Fehlen die der SID, gelten die des Hosts.
- **Abhängigkeiten zwischen Systemen** werden von Hand gepflegt, z. B. wenn ein
  Datenbanksystem von mehreren Systemen genutzt wird. Start geht erst über die
  Abhängigkeiten, Stopp umgekehrt, und eine Abhängigkeit, die ein anderes System noch braucht,
  bleibt laufen. Innerhalb eines Systems ordnen Rollen und Startprioritäten über alle SIDs.
- **Nur eine aktive Aktivität pro System.** Eine Aktivität sperrt alle beteiligten Systeme
  (Tabelle `activity_systems`); weitere werden abgelehnt. Solange sie läuft, lässt sich das
  System auch nicht ändern (Instanzen zuordnen oder entfernen, löschen).
- **Schema und Migrationen:** Das Schema wird mit Alembic verwaltet (`migrations/`).
  `upgrade_database` läuft beim Start (Server und `create_app`): leere Datenbank anlegen,
  Datenbank von vor den Migrationen (Tabelle `schema_info` mit Version 2) übernehmen
  (fehlende Tabellen per `create_all`, dann `stamp 0001`, dann `upgrade`), sonst migrieren.
  Vor dem Migrieren bestehender Daten wird die SQLite-Datei kopiert; die letzten 5 Kopien
  bleiben (Wunsch des Eigentümers: automatisch, mit Sicherung). Schema 1 und unbekannte
  Revisionen werden abgewiesen. `create_all` wird sonst nicht mehr benutzt.
- **Neue Migration anlegen:** Modell ändern, dann gegen eine Datenbank auf neuestem Stand
  automatisch erzeugen, z. B. mit `alembic_config()` aus `db.py` und
  `command.revision(config, message=..., autogenerate=True, rev_id="000N")` (die Verbindung
  über `config.attributes["connection"]`). Die erzeugte Datei prüfen und kürzen. Revisionen
  sind fortlaufend nummeriert (`0001`, `0002`, …). `render_as_batch` ist an, weil SQLite
  Spalten nur durch Neuaufbau der Tabelle ändern kann; bei solchen Migrationen auf die
  Fremdschlüssel achten (`PRAGMA foreign_keys` ist an). Der Test
  `test_models_match_migrations` schlägt fehl, wenn eine Migration fehlt. Autogenerate schlägt
  dabei jedes Mal vor, die Fremdschlüssel von `activities` aus Migration 0004 neu aufzubauen;
  das ist nur die Lücke beim Zurücklesen (siehe Migration 0004) und muss aus der erzeugten
  Datei entfernt werden.
- **Verbindungsdaten eines Hosts** lassen sich ändern (`hosts.update_connection`), nicht aber
  während einer Aktivität, die eine Instanz oder ein System auf dem Host betrifft. Geprüft wird
  die neue Verbindung erst durch die nächste Discovery.
- **Tags** sind Schlüssel mit optionalem Wert (`dev`, `sla=1`), je Schlüssel ein Wert, in den
  Tabellen `host_tags` und `system_tags`. Beim Ersetzen werden vorhandene Schlüssel
  aktualisiert statt gelöscht und neu angelegt (sonst Primärschlüssel-Konflikt im selben
  Flush).
- **Filter** der Landscape: innerhalb eines Filters reicht ein Treffer, alle Filter müssen
  passen; gefiltert wird in Python über die geladenen Listen. Welche Filter sichtbar sind,
  steht pro Benutzer in `user_settings` (Schlüssel `filters.<tab>`, Namen durch Punkte
  getrennt).
- **Benutzer und Rollen** (Wunsch des Eigentümers: vier Rollen wie in LaMa, kein gemeinsames
  Token mehr): viewer < operator < configurator < admin. Welche Rolle eine Route braucht,
  steht zentral in `permissions.required_role` (nach Methode und Routen-Vorlage); API
  (`require_user`) und Oberfläche (`require_login`) prüfen dort. Neue Routen dort einordnen;
  ohne Sonderfall gilt: GET → viewer, sonst configurator. Vorlagen blenden Knöpfe mit
  `can("operator")` usw. aus (Makros mit `import … with context`).
- **Anmeldung:** Sitzungs-Cookie mit Benutzer-ID, `session_epoch` und Ablauf, signiert mit
  einem aus `LO_SECRET_KEY` abgeleiteten Schlüssel. Passwort- oder Rollenänderung und
  Deaktivieren erhöhen `session_epoch` und beenden so alle Sitzungen. Startpasswörter
  (`must_change_password`) erzwingen den Wechsel vor jeder anderen Seite.
- **Erster Admin:** Ohne Benutzer erzeugt `create_app` beim Start einen einmaligen
  Einrichtungscode (`app.state.setup_code`) und gibt ihn auf stderr aus; `/setup` legt damit
  den ersten Admin an. `LO_API_TOKEN` wird nicht mehr verwendet.
- **API-Tokens** sind persönlich, haben die Rechte ihres Benutzers, optional ein Ablaufdatum;
  gespeichert wird nur der SHA-256-Hash. Aktivitäten speichern `requested_by`.
- **Discovery löscht nichts**, sie meldet fehlende Einträge nur.
- **JavaScript ist erlaubt** (Entscheidung des Eigentümers, 2026-11-20), auch ein Framework wie
  React, wo es echten Nutzen bringt: Mehrfachauswahl mit Aktionsleiste, Live-Ansichten,
  Drag-and-drop. Die bisherige Oberfläche funktioniert noch ohne JavaScript; `static/ui.js`
  ergänzt dort nur: offene Systeme im Baum merken (sessionStorage), „Alle auf-/zuklappen“,
  Menüs schließen. Eine neue Oberfläche baut auf der REST-API auf. **Offen:** ob Bibliotheken
  mitgeliefert (gebaut und vom Server ausgeliefert) oder von einem CDN geladen werden; bis zur
  Entscheidung lädt die Oberfläche nichts von außen. Größere Umbauten erst vorschlagen.
- **Gestaltungsrichtung** (Rückmeldung des Eigentümers zu fünf Entwürfen, 2026-11-20):
  - Landscape als dichte Tabelle mit Mehrfachauswahl von Systemen und Instanzen und einer
    Leiste unten mit den möglichen Operationen (Massenoperationen); Befehlspalette (Strg K).
  - Rechts aufklappende Detailleiste eines Systems; darin der Status je Instanz aus
    `GetProcessList` des sapstartsrv (Prozesse mit Farbe). Ein Abhängigkeitsgraph als
    Hauptansicht bringt keinen Mehrwert.
  - Aktivität als Live-Ansicht: Zeitplan je Instanz und mitlaufendes Protokoll mit Filtern.
  - Übersicht als Statusmatrix Produkte × Stufen mit „Braucht Aufmerksamkeit“; was dort
    erscheint, muss konfigurierbar sein (z. B. stündlich geplante Operationen ausblenden).
  - Operationsvorlagen als Kette von Bausteinen mit Parametern je Schritt, Verhalten bei
    Fehler und Einplanen: genau so umsetzen.
- **Systembaum der Landscape:** je System ein `<tbody class="tree-system">`; Auf- und
  Zuklappen per Checkbox und CSS `:has()`, ohne Server. Zugeklappt zu Beginn (Wunsch des
  Eigentümers). Der Filter „Instanztyp“ klappt auf und zeigt nur passende Instanzen
  (`TreeView` in `web/routes.py`), als Grundlage für Massenoperationen.
- **Menü „Operationen“** (`operation_buttons` in `macros.html`, ein `<details class="menu">`)
  statt einzelner Knöpfe; „Status aktualisieren“ bleibt ein eigener Knopf. Die Einträge kommen
  aus dem **Katalog** (`catalog.py`, im Template über `ops("SYSTEM")` usw., gefiltert nach
  Rolle und, bei Instanzen, nach Instanztyp: `ops("INSTANCE", instance)`). Neue eingebaute
  Operationen werden dort eingetragen; eigene kommen aus der Datenbank. Beide erscheinen in
  Menüs, Massenoperationen und `/api/operations`. Routen für
  Operationen brauchen mindestens Operator (`permissions.OPERATION_ROUTES`); die Rolle der
  einzelnen Operation prüft der Handler.
- **Eigene Operationen** (Wunsch des Eigentümers, angelehnt an die kundendefinierten
  Operationen in LaMa): Skripte aus `operations.d` des Host Agents, aufgerufen mit
  `ExecuteOperation` asynchron (Timeout 0), dann `IsOperationFinished` und
  `GetOperationResults` im Takt der Statusabfrage; neue Zeilen kommen ins Protokoll (die
  Übergangszeile „saphostcontrol: Executing … hook“ nicht). Fault 255 heißt „läuft noch“,
  Fault 127 „Skript fehlgeschlagen“ (mit allen Zeilen im Detail `OperationException`),
  -20 „unbekannte Operation“. Nur Admins legen sie an (Tabellen `custom_operations`,
  `custom_operation_parameters`, Migration 0005); jede hat eine eigene Rolle zum Ausführen.
  Die Aktivität (`kind` CUSTOM) speichert die Beschreibung der Operation und die Werte in
  `options`, damit spätere Änderungen laufende Aktivitäten nicht berühren. Parameter: Text,
  Ganzzahl, Ja/Nein; leere optionale Werte werden wie in LaMa nicht übergeben (Wunsch des
  Eigentümers: Aufrücken der Positionsargumente ist Sache des Skripts), fehlende bekommen
  den Standardwert. Text ohne eigenes Muster nur `[A-Za-z0-9._-]*`. `SERVICE_SUBTYPE` wird
  aus dem Instanztyp und den Anwendungsservern des Systems abgeleitet (Annahme). Im Menü
  führen eigene Operationen auf ein Formular (`/run/{instances|systems|hosts}/{id}/{key}`).
  Eingebaute Schlüssel (`start`, `stop`, `restart`, `discover`) sind für eigene gesperrt.
- **Abbrechen** (`OperationRunner.cancel`, Wunsch des Eigentümers): Start, Stopp und Neustart
  hören nur auf zu warten; eigene Operationen werden per `CancelOperation` (O-HARD) auf dem
  Host abgebrochen, ebenso beim Zeitlimit; Massenoperationen brechen laufende Kinder ab und
  überspringen wartende. Endstatus CANCELLED. Ein Abbruch des Benutzers unterbricht eine
  Aktivität nur an **sicheren Stellen** (`_pause` statt `_sleep`, `_wait_for_any`), nie mitten
  in einem Datenbankzugriff: Sonst bleibt die aiosqlite-Verbindung gesperrt und das Ende der
  Aktivität lässt sich nicht schreiben („database is locked“). Welche Aufgaben zu einer
  Aktivität gehören, auch die von `asyncio.gather`, steht in der Kontextvariablen
  `_CURRENT_ACTIVITY`. Neue Wartestellen in Aktivitäten nutzen `_pause`. `cancel` beendet
  vor dem Warten die Lesetransaktion der Anfrage, sonst blockieren sich beide. `_run` schließt
  die Aktivität in einer frischen Sitzung ab. Beim Herunterfahren (`shutdown`) halten
  Aktivitäten ebenfalls an der nächsten sicheren Stelle an („Interrupted“); was nach
  5 Sekunden noch läuft, wird hart abgebrochen.
- **Massenoperationen** (Wunsch des Eigentümers: Systeme, Instanzen und Hosts, Standard 5
  gleichzeitig, bei Fehlern weitermachen, keine zusätzliche Bestätigung): eine
  Sammelaktivität (`target_type` MASS, Einstellungen als JSON in `activities.options`) mit
  Teilaktivitäten (`parent_id`). `_run_mass` startet Kinder bis zur Parallelität, beachtet
  Abhängigkeiten der ausgewählten Systeme, lässt bei Sperrkonflikt auf Geschwister warten und
  überspringt Ziele, die von außen belegt sind. Endstatus SUCCEEDED, PARTIAL oder FAILED.
- **Host-Operationen** (bisher nur Discovery) laufen als Aktivität mit `host_id`; je Host
  höchstens eine gleichzeitig.
- **Migration 0004** fügt Spalten mit Fremdschlüssel per `ALTER TABLE … REFERENCES` an, weil
  Alembics Batch-Modus `activities` neu aufbauen würde und das Löschen der alten Tabelle bei
  aktiven Fremdschlüsseln das Protokoll per `ON DELETE CASCADE` mitlöschen würde. Der
  Vergleichstest ignoriert deshalb das nicht zurückgelesene `ON DELETE` dieser Spalten.
- **Aufbau der Oberfläche:** Übersicht (Kennzahlen), Landscape (beobachten und bedienen:
  Systeme, Hosts), Activities (Aktivitäten, Protokolle), Configuration (einrichten: Hosts,
  Systeme, Pools), Administration (Benutzer; Plattform-Einstellungen folgen), „Mein Konto“
  (Passwort, API-Tokens). Die
  Menüpunkte außer „Übersicht“ heißen in beiden Sprachen englisch, wie vom Eigentümer
  gewünscht.
- **Pools** gruppieren zusammengehörige Systeme (z. B. Entwicklung, Test, Produktion eines
  Produkts). Ein System gehört zu höchstens einem Pool (Tabelle `pool_systems`).
- In `api/app.py` steht bewusst kein `from __future__ import annotations`: FastAPI löst die
  Typangaben der Routen zur Laufzeit auf, und sie verweisen auf lokale Namen.

## Tests

- Die Tests laufen gegen die simulierte Landschaft in `mock/`. `MockLandscapeTransport`
  leitet Anfragen ohne Netzwerk nach Port an die Mock-Dienste weiter.
- Die Fixture `extended_landscape` ergänzt einen Diagnostics Agent `DAA/98` auf `sapapp1` und
  einen Host `sapapp2` mit einer zweiten Instanz `DEV/00`; damit werden Rollen,
  Parallelstart und doppelte Instanznummern getestet.
- Die Standardlandschaft hat zwei SIDs: `DEV` (ABAP) und `HDV` (HANA, eigene SID). Nach der
  Discovery sind alle Instanzen nicht zugeordnet; die Tests der Operationen, der API und der
  Oberfläche legen daraus die Systeme `DEV` und `HDV` an. Ein Test fasst alle drei Instanzen
  in einem System zusammen und prüft die Reihenfolge über SIDs hinweg. Die Fixture
  `single_sid_landscape` legt HANA in `DEV`; sie testet den simulierten `StartSystem`.
- Benutzer für Tests legt `make_user(app, name, role)` aus `tests/conftest.py` an (Passwort
  `PASSWORD`, liefert ein API-Token). Eine autouse-Fixture senkt die scrypt-Kosten.
- `hold_activities(app)` aus `tests/conftest.py` hält laufende Aktivitäten fest, bis das
  zurückgegebene Event gesetzt ist. Tests, die eine noch laufende Aktivität brauchen, nutzen es;
  ohne es kann die Aktivität mit der falschen Uhr schon fertig sein (wackelige Tests).
- Zeit wird über eine falsche Uhr gesteuert (`FakeClock` in `tests/conftest.py`). Tests, die
  Operationen ausführen, übergeben `sleep` und `clock` an `create_app`.
- Eigene Operationen testet `tests/test_custom.py` mit den Fixtures von `test_mass.py` und der
  simulierten Operation `LO_TEST` (`PARAM-SLEEP` Laufzeit, `PARAM-RC` Exit-Code).
- Neue Funktionen brauchen Tests, auch für Fehlerfälle. Verhalten, das der Mock simulieren
  soll, wird zuerst im Mock ergänzt.

## Quellen für die Schnittstellen

Die Nachrichtenformate stammen aus den WSDLs eines Host Agent 7.22 und eines sapstartsrv sowie
aus Beispielausgaben von `saphostctrl`. Die Dateien liegen nicht in diesem Repository, sondern
im privaten Wissens-Repository des Eigentümers (`secondbrain`, Ordner `raw/` und `wiki/`).
Aktuelle WSDLs lassen sich von einem Host abrufen:

- Host Agent: `http://<host>:1128/SAPHostControl/?wsdl`
- sapstartsrv: `http://<host>:5<NR>13/?wsdl`

## Teststand gegen echte Hosts

- Zwei Host Agents ohne SAP-Instanz: Verbindung und Anmeldung mit Basic Auth,
  `GetComputerSystem` (Betriebssystemdaten werden gefunden), `ListInstances` und
  `ListDatabases` mit leeren Listen.
- Ein Host mit ABAP-System und HANA-Datenbank, Discovery nur mit `sapadm`: alle Instanzen
  erkannt; `GetProcessList` und `GetSystemInstanceList` am sapstartsrv funktionieren mit
  `sapadm`; `Database/InstanceName` und `Database/Release` sind gefüllt; SystemDB und Tenant
  erscheinen einzeln; `SAPHostControl-DB-WARNING` kommt vor; HANA hat eine eigene SID.
- `StopSystem` am sapstartsrv mit `sapadm` wird mit „Permission denied“ abgelehnt. Der Mock
  bildet das mit `strict_sapstartsrv_auth` nach. Deshalb laufen Systemoperationen über den
  Host Agent.
- Stopp und Start einer Dialoginstanz über den Host Agent mit `sapadm` funktionieren; das Flag
  `O-INSTANCE` wird akzeptiert, und die Instanz meldet danach alle Prozesse grau bzw. grün.
- Ganze Systeme über den Host Agent funktionieren: ABAP-System (zentrale Dienste und
  Dialoginstanz) nach Startpriorität, HANA-Instanz, und ein vollständiger Neustart mit
  Abhängigkeit vom Datenbanksystem. Der Statuswechsel kann nach der Anforderung einige
  Sekunden auf sich warten lassen.
- Das Systemmodell (Schema 2) funktioniert: Ein System aus zentralen Diensten, Dialoginstanz
  und HANA-Instanz mit eigener SID ließ sich als Ganzes stoppen und starten, einzelne
  Instanzen darin ließen sich neu starten und starten.
- `ExecuteOperation` `versioninfo` funktioniert mit `sapadm` (per `saphostctrl` geprüft); die
  Antwort sind Protokollzeilen mit `kernel release` und `patch number`. Diagnostics Agents
  melden `SMDAGENT`. Zentrale Dienste und Anwendungsserver können auf verschiedenen Hosts
  dieselbe Instanznummer haben.
- Aus dem Tool bestätigt: Die Version des Host Agents wird per SOAP gelesen und richtig
  angezeigt (die Schlüssel der Meldungen sind weiter unbekannt; der Parser ignoriert sie).
  Der Parallelstart von Datenbank und zentralen Diensten funktioniert.
- **Virtuelle Hostnamen:** `StartInstance` nur mit SID und Nummer schlug für eine Instanz mit
  virtuellem Hostnamen mit „SAP Instance not found!“ fehl. Der Host Agent erkennt eine
  Instanz an SID, Nummer und SAPLOCALHOST; das Tool sendet deshalb `mHostname` (Hostname aus
  `ListInstances`), und der Mock verlangt ihn wie der echte Host Agent. Mit `mHostname`
  funktionieren Start, Stopp und Neustart eines über zwei Hosts verteilten Systems.
- **Eigene Operationen** (Testskript mit `Mode: async`, `ResultConverter: hook`):
  `ExecuteOperation` asynchron liefert sofort eine Operation-ID; Argumente kommen als
  `KEY=value`, fehlende fallen weg und folgende rücken auf. `GetOperationResults` während des
  Laufs: Fault 255 „Timeout occurred“ mit den bisherigen Zeilen (zuletzt „saphostcontrol:
  Executing <Description> hook“); `IsOperationFinished` „noch nicht fertig“. Erfolg endet mit
  „… hook successfully executed“; Exit-Code ≠ 0 gibt Fault 127 „Command execution failed.“
  mit „… hook failed (exit code = N)“. `CancelOperation`
  beendet das Skript (Exit-Code 15, Antwort Fault 127); danach `IsOperationFinished` „Failed
  to determine operation status“ und `GetOperationResults` Fault -20 „Operation ID not found“.
- **Rohe Antwort von `GetOperationResults`** (aus dem Tool und per `curl` geprüft): Das
  Antwortelement heißt wie in der WSDL `GetOperationResponse`, nicht
  `GetOperationResultsResponse` (`soapxml.RESPONSE_ELEMENTS`). Jede Zeile kommt als vier
  Meldungen `LogMsg/TimeStamp` (leer), `LogMsg/Severity` (`Info`, `Warning`, `Error`),
  `LogMsg/Source` (Beschreibung der Operation bzw. `saphostcontrol`) und `LogMsg/Text`;
  `hostagent.log_messages` fasst sie zu Zeilen zusammen. `saphostctrl` zeigt dieselben
  Zeilen als „Severity: Source: Text“. Nach der Korrektur läuft eine eigene Host-Operation
  aus dem Tool heraus erfolgreich durch; die Ausgabe des Skripts steht vollständig und in
  der richtigen Reihenfolge im Protokoll. Noch offen aus dem Tool: Fehler mit Exit-Code,
  Abbruch, Operationen auf Instanzen und Systemen.
- Noch nicht gegen echte Systeme getestet: HTTPS.

## Offene Annahmen, die ein Test mit einer echten Instanz klären muss

1. **Flag `O-INSTANCE`:** Geklärt, ein echter Host Agent akzeptiert es.
2. **Operation-ID:** Geklärt für eigene Operationen: `ExecuteOperation` mit Timeout 0 liefert
   sofort eine Operation-ID, `IsOperationFinished`, `GetOperationResults` und
   `CancelOperation` funktionieren damit (Faults 255, 127, -20 wie im Mock). Start und Stopp
   werten ihre Operation-ID weiterhin nicht aus, sie verfolgen den Status der Prozesse.
3. **Zielzustand:** Geklärt für Dialoginstanz, zentrale Dienste und HANA-Instanz (alle
   Prozesse grün bzw. grau).
4. **Geschützte Methoden am sapstartsrv:** Geklärt: Lesen geht mit `sapadm`, `StopSystem`
   nicht. Ob `<sid>adm` für `StartSystem` reicht, ist ohne Belang, weil das Tool es nicht
   mehr nutzt.
5. **Datenbanken:** Der Schlüssel `Database/Vendor` ist angenommen.
6. **Verschachtelung bei `GetComputerSystem`:** Der Client sucht Klassen in jeder Tiefe. Die
   echte Struktur ist nicht mit der des Mocks verglichen.
7. **HTTPS** zu den verwalteten Hosts ist vorbereitet, aber ungetestet.
8. **`GetSystemInstanceList` bei getrennter Datenbank-SID:** Dass die Liste eines
   ABAP-Systems die HANA-Instanz nicht enthält, ist aus den getrennten SIDs abgeleitet, aber
   nicht direkt beobachtet.

Wenn ein Test eine Annahme bestätigt oder widerlegt: Mock, Client und Tests anpassen und den
Abschnitt „Teststand gegen echte Hosts“ hier und im README aktualisieren.

## Bekannte Lücken

- nur lokale Benutzer, kein LDAP oder SSO
- Änderungen an der Konfiguration werden nicht protokolliert, nur Aktivitäten
- Migrationen nur vorwärts; zurück geht es über die Sicherungskopie
- Abbrechen von Start und Stopp beendet nur das Warten
- keine Operationsvorlagen (Ketten von Operationen) und keine zeitgesteuerte Ausführung
- keine eingebaute Operation „Instanz-Agent neu starten und registrieren“
- keine Kernel-Repositories (Ablage der Kernel-Versionen für Kernel-Tausch-Skripte)
- keine Hooks und kein HTTP-Provider für eigene Operationen
- keine eigenen Datenbank-Operationen (`StartDatabase`, `StopDatabase`)
- jede SID nur einmal
- Abhängigkeiten zwischen Systemen werden nicht automatisch erkannt
- eine Instanz gehört zu höchstens einem System; Verschieben geht nur über Entfernen und
  erneutes Zuordnen
- Protokollmeldungen der Aktivitäten sind nur englisch
- kein Verschieben von Instanzen (Adaptive Computing)
- Übersicht nur mit einfachen Kennzahlen; Diagramme und Diagnosenachrichten fehlen noch
