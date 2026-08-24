# Verba — Benutzerhandbuch

Verba wandelt Audioaufnahmen in bearbeitbaren Text um — vollständig lokal, mit
optionaler KI-Bereinigung/Übersetzung, PDF-Export, semantischer Suche und einer
öffentlichen Transkriptions-API.

## Installation & Starten

**Fertige Pakete** (Releases-Seite des Projekts):

- **Windows:** `Verba-Setup-….exe` — Doppelklick, Assistent, Startmenü-Eintrag.
  Die Daten liegen pro Nutzer und überstehen Updates.
- **Linux-Desktop:** `Verba-….AppImage` — ausführbar machen und starten.
- **Linux-Server:** `verba-server-….zip` entpacken und `sudo ./deploy/install.sh`
  ausführen — richtet Dienst (systemd) und Autostart ein; Vorlagen für
  nginx/Caddy liegen bei.

**Aus dem Quellcode:**

- **Windows:** Doppelklick auf `start.bat`
- **Linux:** `./start.sh` im Projektordner
- **Server:** `./start.sh --server --port 8710` — erreichbar über IP/Domain,
  auch hinter einem Reverse Proxy (WebSocket `/ws` durchreichen)

Beim ersten Start werden die Grundkomponenten automatisch eingerichtet; die
Anwendung öffnet sich im Browser unter `http://127.0.0.1:8710`.

Im Desktopmodus beendet der Button **Beenden** oben rechts den lokalen Verba-
Prozess. Wenn das einzige Verba-Browserfenster geschlossen wird, beendet sich
der Desktopserver ebenfalls automatisch. Im Servermodus bleibt Verba dagegen
weiter aktiv.

Verba ist eine **PWA**: Im Browser lässt sich die App „installieren" (Symbol in
der Adressleiste bzw. „Zum Startbildschirm hinzufügen") und fühlt sich dann wie
eine eigenständige App an. Die Oberfläche lädt auch ohne Verbindung; sobald der
Server wieder erreichbar ist, geht es automatisch weiter.

## Ersteinrichtung

Beim ersten Aufruf prüft Verba das System (Python, ffmpeg, GPU,
KI-Komponenten) und installiert Fehlendes automatisch — mit Live-Fortschritt.
Die Einrichtung lässt sich später unter **Einstellungen** erneut aufrufen.

## Transkripte

Jedes Transkript bekommt einen eigenen **Workspace-Ordner** auf der Festplatte mit
`audio/` (importierte Kopien), `transcripts/` (JSON-Transkripte) und `exports/`.

- Neues Transkript: **+**-Knopf (unten rechts). Ohne Namenseingabe wird das heutige
  Datum (`jjjjmmtt`) übernommen.
- Beim Anlegen lässt sich ein **Transkripttyp** wählen (Abschnitt
  „Transkripttypen") — er steuert,
  wie die KI-Aufbereitung den Text behandelt. Ohne Typ liefert das Transkript reinen,
  unformatierten Text.
- Transkripte löschen entfernt auf Wunsch auch den Workspace-Ordner.

Beim Import liest Verba **Metadaten** automatisch aus: Titel und Datum aus
MP3-Tags sowie aus Dateinamen nach dem Schema `JJJJMMTT_Titel` (z. B.
`20240817_Titel.mp3`).

## Transkripttypen

Sechs Standardtypen werden mitgeliefert: **Lied**, **Interview/Dialog**,
**Rede**,  **Protokoll** (Gesprächsprotokoll mit
Zusammenfassung und To-dos), **Gedicht** und **Rollenspiel**. Jeder Typ trägt
einen System-Prompt, der der KI sagt, wie Transkripte dieses Typs aufzubereiten
sind.

Typen werden im eigenen Tab **Typen** (Hauptnavigation) verwaltet: die Liste
zur Auswahl und daneben der Editor für Name und System-Prompt — auf dem
Smartphone nacheinander als Liste und Detailansicht. Der **+**-Knopf legt
einen neuen Typ an; auch Standardtypen lassen sich bearbeiten und löschen.
„Standardtypen wiederherstellen" bringt gelöschte oder veränderte Standards
zurück.

## Audio importieren

Die Aktionskarte im Transkript gliedert den Ablauf in drei Reiter:
**1. Audio importieren → 2. Transkribieren & aufbereiten → 3. Exportieren.**
Ein Tipp auf einen Reiter zeigt genau die Aktionen dieses Schritts; sobald
Dateien vorhanden sind, ist Schritt 2 vorausgewählt.

Drei Wege, alle gleichwertig:

1. **Hochladen** — Dateiauswahl über den Knopf „Dateien hochladen"
2. **Vom Server importieren** — Ordner des Rechners/Servers durchsuchen;
   ein ganzer Ordner importiert alle enthaltenen Audiodateien (auch verschachtelt)
3. **Drag & Drop** — Dateien oder ganze Ordner einfach in die Transkript-Ansicht ziehen

Unterstützte Formate: mp3, wav, m4a, flac, ogg, opus, aac, wma, webm, mp4.
Importieren kopiert immer — die Originaldateien bleiben unangetastet.

## Transkribieren

- **Einzelne Datei:** Mikrofon-Symbol in der Dateizeile (fertige Dateien
  zeigen stattdessen ein Wiederholen-Symbol für einen erneuten Lauf)
- **Alles:** „Alle transkribieren" in Schritt 2 der Aktionskarte
- **Erweitert (aufklappbar):** Whisper-Modell und Aufnahmesprache nur für diesen
  Lauf ändern — die gespeicherten Einstellungen bleiben unberührt
- Fortschritt erscheint live pro Datei; laufende Aufträge sind abbrechbar
- Tipp: Die Aufnahmesprache fest einzustellen (statt automatischer Erkennung)
  verbessert das Ergebnis deutlich

**Warteschlange:** Alle Aufträge laufen über eine zentrale Warteschlange, damit
die Hardware nie überlastet wird — auch wenn mehrere Personen gleichzeitig
arbeiten. Wartende Dateien zeigen ihre Position an; kleine Aufträge (Abschnitt
neu transkribieren, Audio-Schnitt) werden bevorzugt eingeschoben, und die
Reihenfolge bleibt fair pro Nutzer.

## KI-Aufbereitung (Bereinigung & Übersetzung)

Sobald ein Sprachmodell konfiguriert ist (Abschnitt „Sprachmodell (LLM)
einrichten"), erscheint bei
transkribierten Dateien das **Funken-Symbol (KI-Aufbereitung)** und in
Schritt 2 der Aktionskarte der Knopf **KI-Aufbereitung (alle)**:

- **Bereinigen** entfernt Füllwörter und Falschstarts, korrigiert Zeichensetzung
  und offensichtliche Hörfehler — der Transkripttyp fließt als Kontext ein
  (z.B. wird ein „Protokoll" zusammengefasst und mit To-do-Liste versehen)
- **Übersetzen** überträgt den bereinigten Text (oder das Roh-Transkript) in
  nahezu jede Sprache — zur Auswahl stehen alle rund 100 Sprachen, die auch
  Whisper kennt
- Lange Aufnahmen werden automatisch in Abschnitte entlang der Segmentgrenzen
  zerlegt (mit Überlappung), damit auch lokale Modelle mit kleinem Kontext
  sauber arbeiten
- Ergebnisse erscheinen als Reiter im KI-Dialog und im Editor und werden
  zusätzlich als Markdown-Dateien im Workspace unter `transcripts/` abgelegt

**Vollautomatik:** In der Transkript-Ansicht lässt sich **„Automatisch aufbereiten"**
einschalten (optional mit Zielsprache). Dann läuft nach jeder abgeschlossenen
Transkription die Bereinigung — und auf Wunsch die Übersetzung — von selbst an,
ganz ohne weiteren Klick. Manuelles Anstoßen einzelner Schritte bleibt daneben
jederzeit möglich.

**Ablaufplanung:** Läuft das LLM auf einem anderen Rechner (externe API), werden
Transkription und KI-Aufbereitung parallel ausgeführt. Läuft es lokal auf
demselben System, arbeitet Verba phasenweise: erst alle Transkriptionen, dann —
nach einmaligem Modellwechsel — alle KI-Aufbereitungen. So teilen sich Whisper
und LLM den Speicher, ohne sich gegenseitig auszubremsen.

## Editor & Timeline

Aktionen erscheinen als Symbole mit Tooltip (Maus darüber halten zeigt die
Beschreibung). Das Dokument-Symbol („Im Editor öffnen") bei einer fertig
transkribierten Datei öffnet den Editor — einen
**Arbeitsbereich** für Audio, Transkript und KI-Texte:

- **Wellenform** mit Abspielen/Pause; Klick auf einen Segment-Zeitstempel springt
  im Audio dorthin; beim Abspielen wird das aktive Segment hervorgehoben
- **Drei Bereiche** unter der Timeline: *Segmente* (Originaltranskript),
  *Bereinigt* und *Übersetzung* — alle direkt editierbar mit automatischem
  Speichern. Auf großen Bildschirmen stehen die Bereiche **nebeneinander** wie
  in einer Desktop-Anwendung und lassen sich einzeln zu- und abschalten; auf dem
  Smartphone schalten dieselben Reiter zwischen den Ansichten um — der volle
  Funktionsumfang bleibt erhalten.
- Fehlt ein KI-Text noch, lässt er sich **direkt aus dem Editor erstellen**
  („Bereinigung erstellen", „Übersetzung erstellen" mit Sprachwahl); das Ergebnis
  erscheint live im Bereich, sobald es fertig ist
- **Text und Sprecher** direkt in den Segmentzeilen bearbeiten — Änderungen werden
  automatisch gespeichert („Gespeichert"-Anzeige), **Rückgängig** hebt die letzten
  Änderungen schrittweise auf
- **Auswahl** durch Ziehen auf der Wellenform, dann:
  - **Auswahl neu transkribieren** — nur dieser Abschnitt wird neu erkannt und
    ersetzt exakt die betroffenen Segmente
  - **Auf Auswahl kürzen / Auswahl entfernen** — Audio-Schnitt per ffmpeg;
    das Ergebnis entsteht als *neue* Datei im Transkript, das Original bleibt erhalten

## Whisper-Modelle

Im Bereich **Einstellungen → Transkription** zeigt **eine Liste** alle Modelle
mit ihrem Status: Installierte tragen die Markierung „installiert" und lassen
sich direkt löschen, fehlende werden per Klick heruntergeladen. Faustregel:
`small` ist ein guter Start; `large-v3` liefert die beste Qualität, braucht
aber deutlich mehr Leistung. Eigene CTranslate2-Modelle können als Ordner ins
Modellverzeichnis gelegt werden (Unterordner werden erkannt) und erscheinen in
derselben Liste.

## Sprachmodell (LLM) einrichten

Unter **Einstellungen → KI-Aufbereitung (LLM)** wird mit einem Umschalter genau
**ein** Weg gewählt — nur dessen Felder sind sichtbar:

- **Aus** — Bereinigung/Übersetzung deaktiviert, alles andere funktioniert normal
- **Lokal (llama.cpp)** — komplett ohne externe Dienste: Verba zeigt die erkannte
  Hardware (RAM, GPU/VRAM) samt Modellempfehlung; ein Klick installiert llama.cpp,
  ein weiterer lädt das gewünschte Qwen3-Modell. Der lokale LLM-Server startet
  automatisch bei Bedarf.
- **OpenAI-kompatibler Endpunkt** — Base URL, API-Schlüssel und Modellname
  (funktioniert mit OpenAI, Ollama, LM Studio, vLLM u. a.);
  „Verbindung testen" prüft den Endpunkt und listet verfügbare Modelle.

## PDF-Export

Fertig transkribierte Dateien lassen sich als PDF exportieren — über das
PDF-Symbol in der Dateizeile oder in Schritt 3 der Aktionskarte mit
**PDF-Export (alle)** für das ganze
Transkript. Im Dialog wählst du die Textfassung: Original (bereinigter Text,
sonst das rohe Transkript) oder eine vorhandene Übersetzung.

Der Export läuft zweistufig: Mit konfiguriertem LLM strukturiert die KI den
Text passend zum Transkripttyp (Strophen, Sprecherrollen, Protokoll mit
Zusammenfassung und To-dos); ohne LLM entsteht die Struktur regelbasiert —
der Export funktioniert immer. Das Layout richtet sich nach dem Typ, etwa
unsichtbare Trenner und zusätzlicher Leerraum oder ein
Skriptlayout beim Rollenspiel. Ohne Typ entsteht ein schlichtes Text-PDF.

**PDF-Export (alle)** erzeugt ein Sammel-PDF: Jede Datei folgt als eigener
Abschnitt, nur durch Abstand getrennt — ohne Inhaltsverzeichnis und ohne
zusätzliche Titel; die Kopfzeile je Datei (Titel und Datum) kommt aus dem
Template. Fertige PDFs erscheinen in der Karte **Exporte (PDF)** zum
Herunterladen oder Löschen; im Workspace liegen sie unter `exports/`.

## Suche

Der Tab **Suche** durchsucht alle Transkripte gleichzeitig — semantisch (die
Bedeutung zählt; deutsche Fragen finden auch englische oder russische Inhalte)
und per Volltext (Eigennamen und seltene Begriffe treffen exakt). Unter
**Filter** lässt sich die Suche auf ein Transkript, einen Typ, Sprache,
Sprecher und einen Zeitraum einschränken.

Jeder Treffer zeigt Transkript, Datei, Zeitstempel und Textpassage — ein Klick
öffnet den Editor genau an dieser Stelle, das Audio startet dort.

Mit konfiguriertem LLM erzeugt **„KI-Antwort mit Quellen"** eine Antwort, die
jede Aussage mit nummerierten Quellen belegt; die Quellen sind klickbar wie
Treffer. Die KI antwortet ausschließlich aus den gefundenen Passagen — gibt
es keine, sagt sie das ehrlich statt zu raten.

Neue Transkriptionen und Segment-Änderungen werden automatisch indiziert,
gelöschte Dateien sofort aus dem Index entfernt. Unter **Einstellungen →
Suche** stehen der Index-Status, das Embedding-Modell (eine Änderung baut den
Index automatisch komplett neu) und ein Knopf für den manuellen Neuaufbau.
Die Suchkomponenten installiert die Einrichtung (Feature-Gruppe
„Semantische Suche").

## Einstellungen

Die Einstellungen sind in Bereiche gegliedert: Auf dem Smartphone erscheint —
wie in einer nativen App — zuerst eine Liste der Bereiche; ein Tipp öffnet den
Bereich als eigene Seite („Alle Einstellungen" führt zurück). Auf dem Desktop
steht die Bereichsliste als Seitenleiste neben dem gewählten Bereich.

- **Oberfläche:** Sprache (Deutsch, Englisch, Russisch), Dokumentation
- **Transkription:** Standard-Modell, Modellverzeichnis, Gerät (GPU/CPU),
  Rechengenauigkeit, Aufnahmesprache — inklusive der Whisper-Modellverwaltung
  (herunterladen/löschen) im selben Bereich
- **KI-Aufbereitung (LLM):** Aus / Lokal / Endpunkt (Abschnitt „Sprachmodell
  (LLM) einrichten")
- **Speicherorte & Protokolle:** Workspace-Verzeichnis, Server-Port, Log-Level
  und Aufbewahrungsdauer (ältere Logs werden automatisch gelöscht)
- **Suche:** Index-Status, Embedding-Modell, Index neu aufbauen
- **API:** Schlüssel für die öffentliche Transkriptions-API (Abschnitt
  „Öffentliche API")
- **System:** Informationen über den Rechner, auf dem Verba läuft — CPU
  (Modell und Kerne), Arbeitsspeicher (frei/gesamt), Grafikkarte samt VRAM,
  ffmpeg-Status — sowie die App-Version

## Öffentliche API

Verba stellt eine OpenAI-kompatible Transkriptions-API bereit, mit der externe
Programme Audiodateien transkribieren können — Skripte, andere Server oder
alles, was mit dem OpenAI-SDK spricht.

- **Endpunkt:** `POST /v1/audio/transcriptions` (multipart, OpenAI-Format)
- **Antwortformate** über `response_format`: `json` (Standard), `text`, `srt`,
  `vtt` und `verbose_json` (mit Segmenten und Zeitstempeln)
- **Sprache:** optional über `language` (ISO-Code wie `de`), sonst automatische
  Erkennung
- **Modell:** `model=whisper-1` (oder weggelassen) nutzt das in den
  Einstellungen gewählte Whisper-Modell; ein konkreter Name wie `model=small`
  wählt für diese eine Anfrage ein anderes Modell
- **Aufbereitung:** `model=whisper-1+cleanup` liefert vom KI-Modell bereinigten
  Text (erfordert ein konfiguriertes LLM). `project_type=<schlüssel>` (z. B.
  `interview`) bereinigt zusätzlich mit dem Prompt dieses Transkripttyps. In
  `srt`/`vtt`/`verbose_json` bleiben die Segmente die rohen Whisper-Segmente —
  nur der Text ist bereinigt.
- Jede Anfrage läuft durch dieselbe faire Warteschlange wie die App und
  antwortet, sobald die Transkription fertig ist.

**API-Schlüssel:** In den Einstellungen unter „API" lassen sich Schlüssel
erstellen und löschen. Sobald mindestens ein Schlüssel existiert, verlangt der
Endpunkt `Authorization: Bearer <Schlüssel>`; ohne Schlüssel ist die API offen
— das ist nur für rein lokale Nutzung gedacht. Jeder Schlüssel wird genau
einmal im Klartext angezeigt, danach ist nur noch sein Anfang sichtbar.

Beispiel mit curl:

```bash
curl -X POST http://localhost:8710/v1/audio/transcriptions \
  -H "Authorization: Bearer vb-IHRSCHLUESSEL" \
  -F "file=@aufnahme.mp3" \
  -F "language=de" \
  -F "response_format=srt"
```

Beispiel mit dem OpenAI-Python-SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8710/v1", api_key="vb-IHRSCHLUESSEL")
with open("aufnahme.mp3", "rb") as audio:
    result = client.audio.transcriptions.create(model="whisper-1", file=audio)
print(result.text)
```
