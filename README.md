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
| SOAP-Clients für Host Agent und sapstartsrv | geplant |
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

- Die SAPHostControl-Nachrichten sind gegen die WSDL eines Host Agent 7.22 abgeglichen
  (Elementnamen, Enum-Werte). Inhalte, die die WSDL nicht festlegt, sind Beispielwerte, etwa die
  Eigenschaften von `GetComputerSystem` und die Datenbank-Komponenten.
- Die SAPControl-Nachrichten sind der öffentlichen Schnittstellenbeschreibung nachempfunden und
  noch **nicht** gegen eine echte WSDL (`http://<host>:5<NR>13/?wsdl`) geprüft.
- Nur HTTP, kein HTTPS.
- Die Mock-Umgebung ist ein Entwicklungswerkzeug und darf nicht in nicht vertrauenswürdigen
  Netzen erreichbar sein.
