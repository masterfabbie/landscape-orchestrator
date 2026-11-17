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
| SOAP-Clients für Host Agent und sapstartsrv | vorhanden; Host-Agent-Teil gegen echte Hosts bestätigt |
| Inventar (Hosts, SIDs, Instanzen, Datenbanken) mit Discovery und REST-API | vorhanden; gegen echte Hosts bestätigt |
| Systeme als selbst angelegte Gruppen von Instanzen | vorhanden; gegen ein echtes System bestätigt |
| Operationen (Start/Stopp/Neustart) mit Aktivitäten-Log | vorhanden; gegen ein echtes System bestätigt |
| Abhängigkeiten zwischen Systemen | vorhanden; gegen ein echtes System bestätigt |
| Web-Oberfläche (Deutsch und Englisch) | vorhanden |

### Teststand gegen echte Hosts

Bestätigt mit zwei echten SAP Host Agents ohne SAP-Instanz:

- Verbindung zum Endpunkt `/SAPHostControl.cgi` und Anmeldung mit HTTP Basic Auth
- `GetComputerSystem`: Betriebssystem, CPUs und Arbeitsspeicher werden gefunden
- `ListInstances` und `ListDatabases`: leere Listen werden sauber verarbeitet

Bestätigt mit einem echten Host, auf dem ein ABAP-System und eine HANA-Datenbank laufen
(Discovery, nur mit `sapadm` als Zugangsdaten):

- `ListInstances` findet alle Instanzen mit korrekten Instanznummern
- **Lesende Aufrufe am sapstartsrv** (`GetProcessList`, `GetSystemInstanceList`) funktionieren
  mit den Zugangsdaten von `sapadm`; Instanztyp, Startpriorität und Status werden erkannt
- `ListDatabases`: SystemDB und Tenant erscheinen einzeln; die Properties
  `Database/InstanceName` und `Database/Release` sind gefüllt
- Der Datenbankstatus `SAPHostControl-DB-WARNING` kommt in der Praxis vor
- Die HANA-Datenbank hat in der echten Landschaft eine **eigene SID**. Deshalb gibt es die
  Abhängigkeiten zwischen Systemen (siehe „Operationen und Aktivitäten“).
- **`StopSystem` am sapstartsrv mit `sapadm` wird abgelehnt** („Permission denied“). Lesen
  darf `sapadm` am sapstartsrv, administrieren nicht. Deshalb laufen Systemoperationen jetzt
  über den Host Agent (siehe „Operationen und Aktivitäten“).
- **Stopp und Start einer Instanz über den Host Agent mit `sapadm`** funktionieren, das Flag
  `O-INSTANCE` wird akzeptiert. Die Dialoginstanz meldete danach alle Prozesse grau bzw. grün;
  der Stopp dauerte rund 35 Sekunden, der Start rund 15 Sekunden.
- **Ganze Systeme über den Host Agent:** Stopp und Start eines ABAP-Systems mit zentralen
  Diensten und Dialoginstanz in der Reihenfolge der Startpriorität funktionieren, ebenso Start
  und Stopp der HANA-Instanz. Ein vollständiger Neustart mit angelegter Abhängigkeit vom
  Datenbanksystem lief erfolgreich durch.

- **Systeme aus Instanzen mehrerer SIDs:** Ein System aus zentralen Diensten, Dialoginstanz
  und der HANA-Instanz mit eigener SID ließ sich als Ganzes stoppen (rund 56 Sekunden) und
  starten; Neustart und Start einzelner Instanzen dieses Systems funktionieren ebenfalls.
- **Version des Host Agents:** `ExecuteOperation` mit der Operation `versioninfo` funktioniert
  mit `sapadm`. Die Antwort sind Protokollzeilen; das Tool liest daraus `kernel release` und
  `patch number` (angezeigt z. B. als „7.22 PL72“) sowie die Versionszeile von
  `saphostexec`. Achtung: Die Komponentenzeilen nennen einen anderen Patch (Kernel-Patch,
  z. B. 1611) als der Abschnitt „SAPHOSTAGENT information“ (z. B. 72).
- **Diagnostics Agents** melden das Feature `SMDAGENT`.
- Zentrale Dienste und Anwendungsserver eines Systems können auf verschiedenen Hosts dieselbe
  Instanznummer haben.

Noch nicht gegen echte Hosts getestet:

- verteilte Systeme mit Instanzen auf mehreren Hosts
- HTTPS

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

## Web-Oberfläche

Nach dem Start ist die Oberfläche unter `http://127.0.0.1:8000/` erreichbar. Angemeldet wird
mit dem Wert aus `LO_API_TOKEN`.

Die Oberfläche hat fünf Bereiche. **Landscape** dient zum Beobachten und Bedienen,
**Configuration** zum Einrichten.

| Bereich | Inhalt |
|---|---|
| Übersicht | Kennzahlen zu Systemen, Instanzen, Hosts und Aktivitäten der letzten 24 Stunden |
| Landscape | Tab „Systeme“ mit Status, Pool, Abhängigkeiten und Knöpfen für Start, Stopp, Neustart, filterbar nach Pool; Tab „Hosts“ mit Version des Host Agents. Detailseiten für System (Instanzen, Operationen) und Host (Systeminformationen, Instanzen, Datenbanken). Instanzen ohne System erscheinen hier nicht, nur auf der Hostseite ohne Operationen |
| Activities | Tab „Aktivitäten“ mit allen Operationen und Detailseite mit Protokoll; Tab „Protokolle“ mit den letzten 200 Protokolleinträgen |
| Configuration | Tab „Hosts“: anlegen, Discovery, Zugangsdaten, löschen. Tab „Systeme“: nicht zugeordnete Instanzen, Systeme anlegen und zuordnen, Systeme aufklappbar mit ihren Instanzen; Seite eines Systems mit Name, Instanzen, Pool, Abhängigkeiten, Zugangsdaten je SID, Löschen. Tab „Pools“: anlegen und löschen |
| Administration | Versionsstand und aktuelle Einstellungen; Plattform-Einstellungen folgen |

Die Hauptmenüpunkte heißen in beiden Sprachen Landscape, Activities, Configuration und
Administration; nur „Übersicht“ wird übersetzt.

**Pools** fassen Systeme zusammen, die zusammengehören, z. B. Entwicklungs-, Test- und
Produktivsystem eines Produkts. Ein System gehört zu höchstens einem Pool.

### Systeme anlegen

Ein **System** ist eine selbst angelegte Gruppe von Instanzen mit frei wählbarem Namen, z. B.
ein ABAP-System mit der HANA-Instanz seiner Datenbank. Die Instanzen dürfen verschiedene SIDs
haben. Die Discovery legt keine Systeme an; neue Instanzen sind zunächst nicht zugeordnet.

1. Configuration → Hosts: Host anlegen und Discovery starten.
2. Configuration → Systeme: Unter „Nicht zugeordnete Instanzen“ die Instanzen anhaken und
   **Create System** wählen. Die folgende Seite schlägt als Namen die SID der
   Anwendungsinstanzen vor (sonst die erste SID); Name, Beschreibung und Pool lassen sich
   dort festlegen.
3. Weitere Instanzen lassen sich mit **Assign to existing** einem bestehenden System
   zuordnen.

Gehören Instanzen einer SID bereits zu genau einem System, schlägt die Liste für neu
gefundene Instanzen derselben SID dieses System vor. Zugeordnet wird erst per Klick.

Die Spalte „Hängt ab von“ in den aufgeklappten Systemen zeigt bei Anwendungsservern die
anderen SIDs von Datenbank und zentralen Diensten desselben Systems. Zentrale Dienste und
Datenbank hängen von nichts ab. Die Spalte ergibt sich aus den Instanztypen und ist nicht
bearbeitbar.

**Diagnostics Agents** (Feature `SMDAGENT`) gehören zum System, stehen aber in einer eigenen,
zugeklappten Gruppe. Start, Stopp und Neustart des Systems lassen sie aus, und sie zählen
nicht zum Status des Systems. Einzeln lassen sie sich weiter bedienen.

Gibt es dieselbe Kombination aus SID und Instanznummer auf mehreren Hosts, z. B. zentrale
Dienste und Anwendungsserver mit gleicher Nummer, nennt die Bezeichnung zusätzlich den
Hostnamen der Instanz, z. B. `DEV/00 (sapapp2)`. Abhängigkeiten **zwischen** Systemen pflegt man weiterhin auf der Seite eines
Systems.

Wird eine Instanz aus einem System entfernt oder ein System gelöscht, bleiben die Instanzen
im Inventar und sind wieder nicht zugeordnet. Solange für ein System eine Aktivität läuft,
lässt es sich nicht ändern.

- **Sprache:** Deutsch und Englisch, umschaltbar oben rechts. Ohne Auswahl gilt die Sprache
  des Browsers. Protokollmeldungen der Aktivitäten werden auf Englisch aufgezeichnet.
- **Aktualisierung:** Die Seite einer laufenden Aktivität lädt sich alle 2 Sekunden neu,
  Übersicht, Landscape und Systemseite alle 5 Sekunden, solange eine Aktivität läuft.
- **Keine externen Dateien:** Die Oberfläche lädt nichts aus dem Internet und kommt bis auf
  die Rückfrage vor Operationen ohne JavaScript aus.
- **Anmeldung:** Ein signiertes Sitzungs-Cookie, 8 Stunden gültig, `HttpOnly` und
  `SameSite=Strict`. Wird `LO_API_TOKEN` geändert, sind alle Sitzungen ungültig.
  Formulare von fremden Websites werden abgelehnt.

Einschränkungen:

- Es gibt nur das gemeinsame Token, keine Benutzer und Rollen. Wer sich anmelden kann, darf
  alles, auch Systeme stoppen.
- Fehlgeschlagene Anmeldeversuche werden nicht begrenzt.
- Das Cookie wird nur mit dem Attribut `Secure` gesetzt, wenn der Server die Anfrage als
  HTTPS sieht. Hinter einem Reverse-Proxy muss dieser das Protokoll weiterreichen.

## Inventar und REST-API

Das Inventar kennt Hosts, SIDs, Instanzen, Datenbanken sowie Systeme und Pools. Ein Host wird
angelegt und danach per Discovery ausgelesen: Der Host Agent liefert Betriebssystemdaten,
Instanzen samt SID und Datenbanken, der sapstartsrv jeder Instanz liefert Status, Instanztyp und
Startpriorität. Systeme legt man selbst aus Instanzen an (siehe „Systeme anlegen“).

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
| GET | `/api/instances` | alle Instanzen mit SID, System und Zuordnungsvorschlag (`suggested_system_id`); `?unassigned=true` nur nicht zugeordnete |
| GET | `/api/sids` | SIDs mit Angabe, ob Zugangsdaten hinterlegt sind |
| PUT | `/api/sids/{id}/credentials` | Zugangsdaten für den sapstartsrv der Instanzen einer SID setzen |
| GET | `/api/systems` | Systeme mit ihren Instanzen und Abhängigkeiten |
| POST | `/api/systems` | System aus nicht zugeordneten Instanzen anlegen (`{"name": "...", "instance_ids": [ids], "pool_id": id, "description": "..."}`) |
| GET | `/api/systems/{id}` | ein System |
| PATCH | `/api/systems/{id}` | Name und Beschreibung ändern |
| DELETE | `/api/systems/{id}` | System löschen; seine Instanzen werden wieder nicht zugeordnet |
| POST | `/api/systems/{id}/instances` | nicht zugeordnete Instanzen zuordnen (`{"instance_ids": [ids]}`) |
| DELETE | `/api/systems/{id}/instances/{instance_id}` | Instanz aus dem System entfernen |
| PUT | `/api/systems/{id}/dependencies` | Abhängigkeiten eines Systems setzen (`{"depends_on": [ids]}`) |
| PUT | `/api/systems/{id}/pool` | System einem Pool zuordnen (`{"pool_id": id}`, `null` entfernt) |
| GET | `/api/pools` | Pools mit ihren Systemen |
| POST | `/api/pools` | Pool anlegen (`{"name": "...", "description": "..."}`) |
| DELETE | `/api/pools/{id}` | Pool löschen; seine Systeme bleiben erhalten |
| POST | `/api/instances/{id}/start`, `/stop`, `/restart` | Operation für eine Instanz anstoßen; nur für Instanzen eines Systems, sonst HTTP 409 |
| POST | `/api/systems/{id}/start`, `/stop`, `/restart` | Operation für ein ganzes System anstoßen |
| GET | `/api/activities` | Aktivitäten, neueste zuerst (`?limit=`) |
| GET | `/api/activities/{id}` | Aktivität mit Protokoll |

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
  gelten die Zugangsdaten der SID der Instanz, falls gesetzt, sonst die des Hosts. Für den
  Betrieb reicht `sapadm` am Host: Start und Stopp laufen über den Host Agent, und den
  sapstartsrv liest das Tool nur. Zugangsdaten je SID braucht man nur, wenn ein sapstartsrv
  auch das Lesen mit `sapadm` ablehnt. Gepflegt werden sie auf der Seite eines Systems, das
  Instanzen der SID enthält.
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
- Das Datenbankschema wird beim Start angelegt; Migrationen gibt es nicht. Die Datenbank
  trägt eine Schemaversion. **Mit dem neuen Systemmodell (Schema 2) ist eine ältere Datenbank
  nicht mehr verwendbar:** Der Server bricht mit einer Meldung ab. Die Datenbankdatei
  (Standard `landscape.db`) löschen und das Inventar neu einrichten.
- Jede SID gibt es nur einmal. Zwei Landschaften mit gleicher SID lassen sich nicht
  unterscheiden.

## Operationen und Aktivitäten

Jede Operation läuft als Aktivität im Hintergrund. Der Aufruf antwortet sofort mit HTTP 202 und
der Aktivität; den Fortschritt liest man über `/api/activities/{id}`.

```bash
curl -X POST -H "Authorization: Bearer $LO_API_TOKEN" http://127.0.0.1:8000/api/systems/1/stop
```

| Ziel | Weg | Fertig, wenn |
|---|---|---|
| Instanz | Host Agent (`StartInstance` / `StopInstance`) | alle Prozesse grün (Start) bzw. grau (Stopp) |
| System | Host Agent, Instanz für Instanz über alle Instanzen des Systems, auch mehrerer SIDs | alle Instanzen grün bzw. grau |

- **Warum alles über den Host Agent:** `StartSystem` und `StopSystem` des sapstartsrv lehnen
  echte Systeme für `sapadm` ab. Über den Host Agent reicht `sapadm`.
- **Reihenfolge im System:** Sie richtet sich nach der Rolle der Instanz, die das Tool aus
  den Features der Discovery ableitet, und gilt über alle SIDs des Systems.

  | Rolle | erkannt an | Start | Stopp |
  |---|---|---|---|
  | Datenbank | `HDB` | Phase 1 | Phase 2 |
  | Zentrale Dienste | `MESSAGESERVER`, `ENQUE`, `ENQREP` | Phase 1, parallel zur Datenbank | Phase 2, parallel zur Datenbank |
  | Anwendungsserver | `ABAP`, `J2EE`, unbekannte Features | Phase 2 | Phase 1 |
  | Diagnostics Agent | `SMDAGENT` | nicht dabei | nicht dabei |

  Innerhalb einer Rolle ordnet die Startpriorität die Instanzen in Gruppen, z. B. ERS vor
  ASCS. Jede Gruppe muss den Zielzustand erreichen, bevor die nächste ihrer Rolle beginnt,
  und jede Phase muss abgeschlossen sein, bevor die nächste beginnt. Instanzen, die schon im
  Zielzustand sind, werden übersprungen; ist das ganze System schon dort, endet die Aktivität
  sofort.
- **Abhängigkeiten zwischen Systemen:** Ist die Datenbank ein eigenes System, z. B. weil
  mehrere Systeme sie nutzen, legt man auf der Seite des ABAP-Systems fest, dass es vom
  Datenbanksystem abhängt (oder per API: `PUT /api/systems/{id}/dependencies` mit
  `{"depends_on": [<id>]}`). Gehört die HANA-Instanz zum selben System, ist keine
  Abhängigkeit nötig.
  - **Start:** erst die Abhängigkeiten, jeweils bis alle Instanzen grün sind, dann das System.
  - **Stopp:** erst das System, dann seine Abhängigkeiten. Eine Abhängigkeit, die ein
    anderes System noch benötigt, bleibt laufen; das Protokoll vermerkt das.
  - **Stopp eines Datenbanksystems** stoppt die Systeme nicht, die es benötigen. Die sollte
    man vorher stoppen.
  - Abhängigkeiten können verkettet sein. Kreise werden abgelehnt.
  - Eine Aktivität sperrt alle beteiligten Systeme; weitere Operationen darauf werden mit
    HTTP 409 abgelehnt.
- **Neustart** ist Stopp und danach Start. Jede der beiden Phasen hat das volle Zeitlimit.
- **Zeitlimit:** Standard 600 Sekunden, einstellbar mit `?timeout=` (10 bis 7200). Wird der
  Zielzustand nicht erreicht, endet die Aktivität mit `FAILED`. Die Operation auf dem Host
  läuft dabei weiter; abgebrochen wird nur das Warten.
- **Eine Aktivität pro System:** Solange eine Aktivität für ein System, eine seiner
  Instanzen oder ein System läuft, das davon abhängt, werden weitere mit HTTP 409 abgelehnt.
- **Status der Aktivität:** `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`. Das Protokoll hält
  jede Statusänderung fest, und der Status im Inventar wird dabei aktualisiert.
- **Datenbanken:** Am Ende jeder Aktivität wird der Status der Datenbanken auf den
  beteiligten Hosts neu gelesen, damit er zum Zustand der Instanzen passt.
- **Fehlermeldungen:** Lehnt ein Dienst einen Aufruf ab, nennt die Meldung den Aufruf, den
  Dienst, die verwendeten Zugangsdaten und was zu tun ist, z. B. dass für die SID
  Zugangsdaten von `<sid>adm` fehlen.
- **Serverneustart:** Aktivitäten, die beim Beenden des Servers noch liefen, werden als
  `FAILED` mit dem Hinweis „Interrupted“ vermerkt. Der Zustand des Ziels ist dann unbekannt
  und sollte per Discovery geprüft werden.

Einschränkungen:

- Laufende Aktivitäten lassen sich noch nicht abbrechen.
- Abhängigkeiten werden nicht automatisch erkannt, sondern von Hand gepflegt.
- Operationen für Datenbanken ohne SAP-Instanz (`StartDatabase` / `StopDatabase`) fehlen noch.
  Eine HANA-Datenbank wird über ihre Instanz gestartet und gestoppt.

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
| `HostAgentClient` | `list_instances`, `list_databases`, `get_database_status`, `get_computer_system`, `execute_operation`, `get_version`, `start_instance`, `stop_instance` |
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

`start_instance` und `stop_instance` senden standardmäßig das Flag `O-INSTANCE`; ein echter
Host Agent akzeptiert das. `SapControlClient` kann weiterhin `StartSystem` und `StopSystem`
aufrufen, die Operationen nutzen das aber nicht mehr. Der übrige Teststand steht oben unter
„Teststand gegen echte Hosts“.

## Mock-Umgebung

Die Mock-Umgebung simuliert eine kleine Landschaft, damit ohne echte SAP-Systeme entwickelt
und getestet werden kann. Sie startet pro Host einen Host-Agent-Endpunkt und pro Instanz einen
sapstartsrv-Endpunkt.

```bash
.venv/bin/lo-mock
```

Die Standardlandschaft enthält das ABAP-System `DEV` und, wie in echten Landschaften, ein
eigenes HANA-System `HDV`:

| Host | Endpunkt | Port |
|---|---|---|
| `sapapp1` | Host Agent | 1128 |
| `sapapp1` | ASCS01 | 50113 |
| `sapapp1` | Dialoginstanz D00 | 50013 |
| `sapdb1` | Host Agent | 11128 |
| `sapdb1` | HANA-Instanz 02 (System `HDV`) | 60213 |

Die HANA-Instanz meldet wie ein echtes Multi-Tenant-System zwei Datenbanken: `SYSTEMDB@HDV`
und `HDV@HDV`. Nach der Discovery sind alle Instanzen nicht zugeordnet; Systeme legt man
selbst an, z. B. ein System `DEV` aus allen drei Instanzen.

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

Mögliche Instanztypen (`kind`): `HDB`, `ERS`, `ASCS`, `SCS`, `ABAP`, `J2EE`, `SMDAGENT`
(Diagnostics Agent; Startpriorität und Prozessnamen sind angenommen).

### Simulierte Webmethoden

| Dienst | Methoden |
|---|---|
| SAPControl | GetProcessList, GetSystemInstanceList, Start, Stop, RestartInstance, StartSystem, StopSystem, RestartSystem |
| SAPHostControl | ListInstances, ListDatabases, GetDatabaseStatus, GetComputerSystem, StartInstance, StopInstance, ExecuteOperation (nur `versioninfo`) |

Verhalten:

- Mit `"strict_sapstartsrv_auth": true` erlaubt der simulierte sapstartsrv administrative
  Aufrufe nur `<sid>adm` und antwortet anderen bekannten Benutzern mit „Permission denied“,
  wie ein echtes System. Standard ist `false`.
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
  genaue Verschachtelung der CIM-Klassen sowie der Property-Schlüssel für den Vendor.
- Die rohen Antworten echter Hosts sind noch nicht mit denen des Mocks verglichen. Der Client
  findet die Betriebssystemdaten echter Hosts; er sucht die Klassen aber in jeder
  Verschachtelungstiefe, die Struktur selbst ist damit nicht belegt.
- Nur HTTP, kein HTTPS.
- Die Mock-Umgebung ist ein Entwicklungswerkzeug und darf nicht in nicht vertrauenswürdigen
  Netzen erreichbar sein.
