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

Server gegen die simulierte Landschaft, braucht `LO_SECRET_KEY` und `LO_API_TOKEN`
(erzeugen mit `.venv/bin/lo-keygen`):

```bash
.venv/bin/lo-server --mock
```

Vor jedem Commit müssen Tests und Lint grün sein. Die CI prüft beides auf Python 3.11 und 3.13.

## Aufbau

Alles liegt unter `src/landscape_orchestrator/`.

| Modul | Aufgabe |
|---|---|
| `soapxml.py` | SOAP-Envelopes bauen und lesen, gemeinsam für Clients und Mock |
| `clients/` | asynchrone SOAP-Clients: `HostAgentClient` (SAPHostControl, Port 1128/1129), `SapControlClient` (sapstartsrv, Port 5NN13/5NN14), gemeinsame Fehlerklassen in `base.py` |
| `models.py`, `db.py` | Datenmodell (SQLAlchemy, asynchron): Host, Sid, Instance, System, Pool, Database, Activity, ActivityLogEntry; Schemaversion und Prüfung beim Start |
| `crypto.py`, `settings.py` | Verschlüsselung gespeicherter Passwörter (Fernet), Konfiguration aus Umgebungsvariablen |
| `connections.py` | baut Clients für Inventareinträge samt Zugangsdaten |
| `discovery.py` | liest einen Host aus und aktualisiert das Inventar |
| `dependencies.py` | Abhängigkeiten zwischen Systemen, Reihenfolge von Start und Stopp |
| `roles.py` | Rolle einer Instanz aus ihren Features: Datenbank, zentrale Dienste, Anwendungsserver, Diagnostics Agent |
| `systems.py` | Systeme als Gruppen von Instanzen: anlegen, zuordnen, entfernen, löschen, Namens- und Zuordnungsvorschläge, abgeleitete „Hängt ab von“-SIDs |
| `pools.py` | Zuordnung von Systemen zu Pools |
| `operations.py` | Start, Stopp, Neustart als Hintergrund-Aktivitäten (`OperationRunner`) |
| `api/` | REST-API unter `/api`, geschützt durch ein Bearer-Token; `create_app` baut die Anwendung |
| `web/` | servergerenderte Oberfläche (Jinja2), Sitzungs-Cookie, Übersetzungen |
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
- **Schema:** Es gibt keine Migrationen. Kompatible Erweiterungen nur durch neue Tabellen;
  `create_all` legt sie beim Start auch in bestehenden Datenbanken an, neue Spalten in
  bestehenden Tabellen nicht. Für inkompatible Änderungen `SCHEMA_VERSION` in `models.py`
  erhöhen; der Server weist ältere Datenbanken dann mit einer Meldung ab (Tabelle
  `schema_info`). Schema 2 (Systeme als Gruppen) war ein solcher Bruch; der Eigentümer hat
  dafür seine Daten gelöscht.
- **Discovery löscht nichts**, sie meldet fehlende Einträge nur.
- **Die Oberfläche lädt keine externen Dateien** und nutzt kein JavaScript-Framework.
- **Aufbau der Oberfläche:** Übersicht (Kennzahlen), Landscape (beobachten und bedienen:
  Systeme, Hosts), Activities (Aktivitäten, Protokolle), Configuration (einrichten: Hosts,
  Systeme, Pools), Administration (Plattform-Einstellungen, noch weitgehend leer). Die
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
- Zeit wird über eine falsche Uhr gesteuert (`FakeClock` in `tests/conftest.py`). Tests, die
  Operationen ausführen, übergeben `sleep` und `clock` an `create_app`.
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
- Noch nicht gegen echte Systeme getestet: verteilte Systeme mit Instanzen auf mehreren
  Hosts, die parallele Startreihenfolge, `ExecuteOperation` über SOAP aus dem Tool (die
  Schlüssel der Meldungen sind unbekannt; der Parser ignoriert sie).

## Offene Annahmen, die ein Test mit einer echten Instanz klären muss

1. **Flag `O-INSTANCE`:** Geklärt, ein echter Host Agent akzeptiert es.
2. **Operation-ID:** Der Client ruft Start und Stopp mit Timeout 0 (asynchron) auf und wertet
   die zurückgegebene Operation-ID nicht aus. `GetOperationResults` ist nicht umgesetzt.
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

- keine Benutzer und Rollen, nur ein gemeinsames Token
- keine Schema-Migrationen; Tabellen werden beim Start angelegt
- Aktivitäten lassen sich nicht abbrechen
- keine eigenen Datenbank-Operationen (`StartDatabase`, `StopDatabase`)
- jede SID nur einmal
- Abhängigkeiten zwischen Systemen werden nicht automatisch erkannt
- eine Instanz gehört zu höchstens einem System; Verschieben geht nur über Entfernen und
  erneutes Zuordnen
- Protokollmeldungen der Aktivitäten sind nur englisch
- kein Verschieben von Instanzen (Adaptive Computing)
- Übersicht nur mit einfachen Kennzahlen; Diagramme und Diagnosenachrichten fehlen noch
- Verbindungsdaten eines Hosts lassen sich nach dem Anlegen nicht ändern (nur Zugangsdaten)
