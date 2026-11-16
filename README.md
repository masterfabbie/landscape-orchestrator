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
| Inventar (Hosts, Systeme, Instanzen, Datenbanken) | geplant |
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
