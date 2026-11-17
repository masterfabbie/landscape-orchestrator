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
| `models.py`, `db.py` | Datenmodell (SQLAlchemy, asynchron): Host, System, Instance, Database, Activity, ActivityLogEntry |
| `crypto.py`, `settings.py` | Verschlüsselung gespeicherter Passwörter (Fernet), Konfiguration aus Umgebungsvariablen |
| `connections.py` | baut Clients für Inventareinträge samt Zugangsdaten |
| `discovery.py` | liest einen Host aus und aktualisiert das Inventar |
| `dependencies.py` | Abhängigkeiten zwischen Systemen, Reihenfolge von Start und Stopp |
| `operations.py` | Start, Stopp, Neustart als Hintergrund-Aktivitäten (`OperationRunner`) |
| `api/` | REST-API unter `/api`, geschützt durch ein Bearer-Token; `create_app` baut die Anwendung |
| `web/` | servergerenderte Oberfläche (Jinja2), Sitzungs-Cookie, Übersetzungen |
| `mock/` | simulierter Host Agent und sapstartsrv für Entwicklung und Tests |

Wichtige Entwurfsentscheidungen:

- **Instanzen** werden über den Host Agent gestartet und gestoppt (`StartInstance`,
  `StopInstance`), **ganze Systeme** über `StartSystem` und `StopSystem` des sapstartsrv.
- **Zugangsdaten** gibt es auf zwei Ebenen: Die des Hosts gelten für den Host Agent, die des
  Systems für den sapstartsrv. Fehlen die des Systems, gelten die des Hosts.
- **Datenbanken haben in echten Landschaften eine eigene SID.** Ein ABAP-System hängt dann vom
  Datenbanksystem ab. Abhängigkeiten werden von Hand gepflegt; Start geht erst über die
  Abhängigkeiten, Stopp umgekehrt, und eine Abhängigkeit, die ein anderes System noch braucht,
  bleibt laufen.
- **Nur eine aktive Aktivität pro System.** Eine Aktivität sperrt alle beteiligten Systeme
  (Tabelle `activity_systems`); weitere werden abgelehnt.
- **Schema nur durch neue Tabellen erweitern**, solange es keine Migrationen gibt. Neue
  Tabellen legt `create_all` beim Start auch in bestehenden Datenbanken an; neue Spalten in
  bestehenden Tabellen nicht.
- **Discovery löscht nichts**, sie meldet fehlende Einträge nur.
- **Die Oberfläche lädt keine externen Dateien** und nutzt kein JavaScript-Framework.
- In `api/app.py` steht bewusst kein `from __future__ import annotations`: FastAPI löst die
  Typangaben der Routen zur Laufzeit auf, und sie verweisen auf lokale Namen.

## Tests

- Die Tests laufen gegen die simulierte Landschaft in `mock/`. `MockLandscapeTransport`
  leitet Anfragen ohne Netzwerk nach Port an die Mock-Dienste weiter.
- Die Standardlandschaft hat zwei Systeme: `DEV` (ABAP) und `HDV` (HANA, eigene SID). Die
  Fixture `single_sid_landscape` legt HANA in `DEV`; damit wird die Startreihenfolge innerhalb
  eines Systems getestet.
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
- Start, Stopp und Abhängigkeiten sind noch nicht gegen echte Systeme getestet.

## Offene Annahmen, die ein Test mit einer echten Instanz klären muss

1. **Flag `O-INSTANCE`:** `start_instance` und `stop_instance` senden es standardmäßig. Die
   WSDL nennt das Flag, sagt aber nicht, ob es nötig ist.
2. **Operation-ID:** Der Client ruft Start und Stopp mit Timeout 0 (asynchron) auf und wertet
   die zurückgegebene Operation-ID nicht aus. `GetOperationResults` ist nicht umgesetzt.
3. **Zielzustand:** Eine Operation gilt als fertig, wenn alle Prozesse grün (Start) bzw. grau
   (Stopp) sind. Ob echte Instanzen das so melden, ist offen.
4. **Geschützte Methoden am sapstartsrv:** Lesende Aufrufe funktionieren mit `sapadm`.
   Offen ist, ob `StartSystem` und `StopSystem` mit `sapadm` erlaubt sind oder `<sid>adm`
   brauchen.
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
- Protokollmeldungen der Aktivitäten sind nur englisch
- kein Verschieben von Instanzen (Adaptive Computing)
