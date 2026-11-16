# Landscape Orchestrator

Ein Werkzeug zur zentralen Verwaltung von SAP-Systemlandschaften. Es spricht mit dem
**SAP Host Agent** (Webservice SAPHostControl, Port 1128/1129) und dem **SAP Start Service**
(`sapstartsrv`, Webservice SAPControl, Port 5NN13/5NN14) auf den verwalteten Hosts.

Dies ist ein unabhängiges Projekt und kein SAP-Produkt.

## Stand

| Baustein | Status |
|---|---|
| Projektgerüst, CI | vorhanden |
| Mock-Umgebung (simulierter Host Agent und sapstartsrv) | vorhanden |
| SOAP-Clients für Host Agent und sapstartsrv | vorhanden, gegen den Mock getestet |
| Inventar (Hosts, Systeme, Instanzen, Datenbanken) mit Discovery und REST-API | vorhanden, gegen den Mock getestet |
| Operationen (Start/Stopp/Neustart) mit Aktivitäten-Log | geplant |
| Web-UI | geplant |

Nicht Teil der ersten Version: Verschieben von Instanzen (Adaptive Computing),
kundendefinierte Operationen und Hooks.

## Entwicklung

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/pytest
```

```bash
.venv/bin/ruff check .
```

## Server starten

Der Server braucht zwei Werte aus Umgebungsvariablen. `lo-keygen` erzeugt beide:

```bash
.venv/bin/lo-keygen
```

| Variable | Bedeutung |
|---|---|
| `LO_SECRET_KEY` | Schlüssel, mit dem gespeicherte Passwörter verschlüsselt werden |
| `LO_API_TOKEN` | Token für den Zugriff auf die REST-API (mindestens 16 Zeichen) |
| `LO_DATABASE_URL` | optional, Standard `sqlite+aiosqlite:///./landscape.db` |

**Den `LO_SECRET_KEY` sicher aufbewahren.** Geht er verloren oder wird er geändert, lassen sich
die gespeicherten Passwörter nicht mehr entschlüsseln und müssen neu eingegeben werden.

```bash
.venv/bin/lo-server
```

Der Server lauscht standardmäßig nur auf `127.0.0.1:8000`. Mit `--mock` spricht er statt echter
Hosts die eingebaute simulierte Landschaft an:

```bash
.venv/bin/lo-server --mock
```

## Inventar und REST-API

Das Inventar kennt Hosts, SAP-Systeme (SID), Instanzen und Datenbanken. Ein Host wird angelegt
und danach per Discovery ausgelesen: Der Host Agent liefert Betriebssystemdaten, Instanzen und
Datenbanken, der sapstartsrv jeder Instanz liefert Status, Instanztyp und Startpriorität.

Alle Aufrufe unter `/api` verlangen den Header `Authorization: Bearer <LO_API_TOKEN>`.
Die interaktive Schnittstellenbeschreibung unter `/api/docs` ist ohne Token lesbar; sie enthält
keine Inventardaten.

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/health` | Lebenszeichen, ohne Token |
| GET | `/api/hosts` | Hosts auflisten |
| POST | `/api/hosts` | Host anlegen |
| GET | `/api/hosts/{id}` | Host mit Instanzen und Datenbanken |
| PUT | `/api/hosts/{id}/credentials` | Zugangsdaten des Hosts setzen |
| DELETE | `/api/hosts/{id}` | Host samt Instanzen und Datenbanken löschen |
| POST | `/api/hosts/{id}/discover` | Host auslesen und Inventar aktualisieren |
| GET | `/api/systems` | Systeme mit ihren Instanzen über alle Hosts |
| PUT | `/api/systems/{id}/credentials` | Zugangsdaten für den sapstartsrv eines Systems setzen |

Beispiel mit der simulierten Landschaft:

```bash
curl -H "Authorization: Bearer $LO_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "sapapp1", "address": "127.0.0.1", "username": "devadm", "password": "mock"}' \
  http://127.0.0.1:8000/api/hosts
```

```bash
curl -X POST -H "Authorization: Bearer $LO_API_TOKEN" http://127.0.0.1:8000/api/hosts/1/discover
```

Verhalten:

- **Zugangsdaten:** Passwörter werden verschlüsselt gespeichert und von der API nie
  zurückgegeben. Die Zugangsdaten des Hosts gelten für den Host Agent. Für den sapstartsrv
  gelten die Zugangsdaten des Systems, falls gesetzt, sonst die des Hosts.
- **Discovery löscht nichts.** Instanzen und Datenbanken, die der Host nicht mehr meldet,
  bleiben im Inventar und werden im Ergebnis als fehlend aufgeführt.
- **Nicht erreichbarer Host Agent:** Die Discovery antwortet mit HTTP 502, der Host wird als
  nicht erreichbar markiert.
- **Nicht erreichbarer sapstartsrv:** Die Instanz wird trotzdem aufgenommen, mit Status
  `UNKNOWN` und einer Warnung im Ergebnis.
- Der gespeicherte Status ist der Stand der letzten Discovery, kein Live-Wert.
- `sapcontrol_port_offset` verschiebt die sapstartsrv-Ports eines Hosts. Das ist nur für
  Testaufbauten gedacht, in denen mehrere simulierte Hosts auf einem Rechner laufen.

Einschränkungen:

- Die API kennt nur ein gemeinsames Token, keine Benutzer und Rollen.
- Der Server selbst spricht nur HTTP. Für den Zugriff über ein Netzwerk gehört ein
  Reverse-Proxy mit TLS davor.
- Das Datenbankschema wird beim Start angelegt. Schemaänderungen an einer bestehenden
  Datenbank (Migrationen) sind noch nicht vorgesehen.
- Jede SID gibt es nur einmal. Zwei Systeme mit gleicher SID lassen sich nicht unterscheiden.

## SOAP-Clients

Die Clients sind asynchron und liegen in `landscape_orchestrator.clients`.

```python
from landscape_orchestrator.clients import HostAgentClient, SapControlClient

async with HostAgentClient("sapapp1", auth=("sapadm", "...")) as agent:
    instances = await agent.list_instances()
    databases = await agent.list_databases()
    system = await agent.get_computer_system()

async with SapControlClient("sapapp1", 0, auth=("devadm", "...")) as sap:
    processes = await sap.get_process_list()
    await sap.start()
```

| Client | Methoden |
|---|---|
| `HostAgentClient` | `list_instances`, `list_databases`, `get_database_status`, `get_computer_system`, `start_instance`, `stop_instance` |
| `SapControlClient` | `get_process_list`, `get_system_instance_list`, `start`, `stop`, `restart_instance`, `start_system`, `stop_system`, `restart_system` |

- Ports: Host Agent 1128 (HTTP) bzw. 1129 (HTTPS), sapstartsrv 5NN13 bzw. 5NN14. Mit `https=True`
  wird HTTPS genutzt, mit `port=` lässt sich der Port überschreiben.
- `verify` steuert die Zertifikatsprüfung bei HTTPS: `True` (Standard), Pfad zu einem CA-Bundle
  oder `False`.
- Start und Stopp kehren sofort zurück. Den Fortschritt liest man über `get_process_list` bzw.
  `get_system_instance_list`.
- Umgebungsvariablen für Proxys werden ignoriert, verwaltete Hosts werden direkt angesprochen.

Fehler sind Unterklassen von `SoapError`:

| Fehler | Bedeutung |
|---|---|
| `SoapConnectionError` | Host nicht erreichbar oder Zeitüberschreitung |
| `AuthenticationRequiredError` | geschützte Methode ohne Anmeldedaten (HTTP 401) |
| `InvalidCredentialsError` | Benutzer oder Passwort falsch |
| `PermissionDeniedError` | Benutzer nicht berechtigt |
| `SoapFaultError` | sonstiger Fehler des Dienstes, mit `faultstring` |
| `SoapProtocolError` | Antwort ist kein gültiges SOAP oder enthält unbekannte Werte |

Noch offen: Die Clients sind bisher nur gegen den Mock getestet, nicht gegen echte Hosts.
`start_instance` und `stop_instance` senden standardmäßig das Flag `O-INSTANCE`; ob der Host
Agent das so erwartet, muss der erste Test gegen einen echten Host zeigen.

## Mock-Umgebung

Die Mock-Umgebung simuliert eine kleine Landschaft, damit ohne echte SAP-Systeme entwickelt
und getestet werden kann. Sie startet pro Host einen Host-Agent-Endpunkt und pro Instanz einen
sapstartsrv-Endpunkt.

```bash
.venv/bin/lo-mock
```

Die Standardlandschaft enthält das System `DEV`:

| Host | Endpunkt | Port |
|---|---|---|
| `sapapp1` | Host Agent | 1128 |
| `sapapp1` | ASCS01 | 50113 |
| `sapapp1` | Dialoginstanz D00 | 50013 |
| `sapdb1` | Host Agent | 11128 |
| `sapdb1` | HANA-Instanz 02 | 60213 |

Die HANA-Instanz meldet wie ein echtes Multi-Tenant-System zwei Datenbanken: `SYSTEMDB@DEV`
und `DEV@DEV`.

Alle Endpunkte lauschen auf `127.0.0.1`. Weil die Mock-Hosts auf demselben Rechner laufen,
verschiebt `port_offset` die Ports des zweiten Hosts um 10000. Benutzer für geschützte
Methoden: `devadm` und `sapadm`, Passwort jeweils `mock`.

Eine eigene Landschaft lässt sich als JSON-Datei übergeben:

```bash
.venv/bin/lo-mock --config landschaft.json
```

```json
{
  "start_duration": 3,
  "stop_duration": 2,
  "users": {"devadm": "mock"},
  "hosts": [
    {
      "name": "sapapp1",
      "port_offset": 0,
      "instances": [{"sid": "DEV", "nr": 0, "kind": "ABAP", "running": true}],
      "databases": []
    }
  ]
}
```

Mögliche Instanztypen (`kind`): `HDB`, `ERS`, `ASCS`, `SCS`, `ABAP`, `J2EE`.

### Simulierte Webmethoden

| Dienst | Methoden |
|---|---|
| SAPControl | GetProcessList, GetSystemInstanceList, Start, Stop, RestartInstance, StartSystem, StopSystem, RestartSystem |
| SAPHostControl | ListInstances, ListDatabases, GetDatabaseStatus, GetComputerSystem, StartInstance, StopInstance |

Verhalten:

- Start und Stopp laufen asynchron wie beim echten Dienst: Der Aufruf kehrt sofort zurück,
  die Instanz ist danach `start_duration` bzw. `stop_duration` Sekunden lang gelb.
- `StartSystem` startet nach Instanzpriorität (HDB, dann ASCS, dann Anwendungsserver),
  `StopSystem` in umgekehrter Reihenfolge.
- Zustandsändernde Methoden verlangen HTTP Basic Auth. Ohne Anmeldedaten kommt HTTP 401,
  mit falschen ein SOAP Fault „Invalid Credentials“.

### Einschränkungen der Mock-Umgebung

- Elementnamen und Enum-Werte beider Dienste sind gegen die WSDLs eines Host Agent 7.22 und
  eines sapstartsrv abgeglichen.
- Inhalte, die die WSDLs offenlassen, orientieren sich an echten `saphostctrl`-Ausgaben: die
  CIM-Klassen von `GetComputerSystem` und die Datenbank-Komponenten. Annahmen bleiben die
  Verschachtelung der CIM-Klassen sowie die Property-Schlüssel für Vendor und Release.
- Gegen einen echten Host getestet ist der Mock noch nicht.
- Nur HTTP, kein HTTPS.
- Die Mock-Umgebung ist ein Entwicklungswerkzeug und darf nicht in nicht vertrauenswürdigen
  Netzen erreichbar sein.
