# Verba — Benutzerhandbuch

Verba wandelt Audioaufnahmen in bearbeitbaren Text um — vollständig lokal, mit
optionaler KI-Bereinigung/Übersetzung, PDF-Export, semantischer Suche und einer
öffentlichen Transkriptions-API.

## Installation & Starten {#install}

**Fertige Pakete** (Releases-Seite des Projekts):

- **Windows:** `Verba-Setup-….exe` — Doppelklick, Assistent, Startmenü-Eintrag.
  Für ein Update einfach den neuen Installer über die bestehende Installation
  ausführen; die Daten liegen pro Nutzer und überstehen Updates.
- **Linux-Desktop:** `Verba-….AppImage` — ausführbar machen und starten.
- **Linux-Server:** `verba-server-….zip` entpacken und `sudo ./deploy/install.sh`
  ausführen — richtet Dienst (systemd) und Autostart ein; Vorlagen für
  nginx/Caddy liegen bei. Bei einer bestehenden Installation aktualisiert
  derselbe Befehl Verba automatisch und erhält Daten und Arbeitsbereiche.

**Aus dem Quellcode:**

- **Windows:** Doppelklick auf `start.bat`
- **Linux:** `./start.sh` im Projektordner
- **Server:** `./start.sh --server --port 8710` — erreichbar über IP/Domain,
  auch hinter einem Reverse Proxy (WebSocket `/ws` durchreichen)

Beim ersten Start werden die Grundkomponenten automatisch eingerichtet; die
Anwendung öffnet sich im Browser unter `http://127.0.0.1:8710`.

**Adresse beim Start.** Jeder Start schreibt in die Konsole, worauf Verba
hört — im Servermodus mit allen IP-Adressen des Rechners, damit von einem
anderen Gerät klar ist, wohin:

```
------------------------------------------------------
Verba 0.1.0 - server mode
  listening on   http://0.0.0.0:8710  (all interfaces)
  local          http://127.0.0.1:8710
  network        http://192.168.1.50:8710
  data directory /opt/verba/data
  stop with Ctrl+C
------------------------------------------------------
```

Als **Dienst** (systemd) landet derselbe Block im Journal:
`systemctl status verba` zeigt ihn am Ende, `journalctl -u verba` von Anfang
an. Zusätzlich steht die Adresse in der ersten Zeile des Anwendungsprotokolls
(`data/logs/`), also auch bei einem Dienst, der seit Wochen läuft. Ist der
Port belegt, sagt Verba das in einer Zeile und startet nicht — statt mit
einem Python-Stacktrace abzubrechen.

Im Desktopmodus schließt der Knopf **✕** oben rechts den Verba-Tab und
beendet den lokalen Prozess. Auch ohne diesen Knopf beendet sich der
Desktopserver: sobald der letzte Verba-Tab (oder der ganze Browser)
geschlossen ist, hält er sich noch wenige Sekunden für ein Neuladen offen und
beendet sich dann von selbst. Im Servermodus bleibt Verba dagegen weiter
aktiv, bis der Dienst gestoppt wird.

Verba ist eine **PWA**: Im Browser lässt sich die App „installieren" (Symbol in
der Adressleiste bzw. „Zum Startbildschirm hinzufügen") und fühlt sich dann wie
eine eigenständige App an. Die Oberfläche lädt auch ohne Verbindung; sobald der
Server wieder erreichbar ist, geht es automatisch weiter.

## Ersteinrichtung {#first-run}

Die Ersteinrichtung führt in sechs Schritten durch alles, was Verba braucht:

1. **Komponenten installieren** — Verba prüft das System (Python, ffmpeg, GPU,
   KI-Komponenten) und installiert Fehlendes automatisch. Der Fortschritt läuft
   live mit: der Balken zeigt die gesamte Einrichtung, und jede Komponente
   bekommt ihren Haken, sobald sie installiert und geprüft ist — auch die
   optionale semantische Suche, die deshalb jederzeit nachinstallierbar
   bleibt. Während die
   Installation läuft, sind **Weiter**, **Schritt überspringen** und **Später
   einrichten** gesperrt — sie werden wieder nutzbar, sobald die Installation
   fertig ist oder mit einem Fehler endet.
2. **Arbeitsbereich** — wohin Verba die Transkript-Ordner legt (Abschnitt
   „Transkripte").
3. **Transkription** — Standard-Modell, Modellverzeichnis, Gerät und
   Aufnahmesprache (Abschnitt „Whisper-Modelle").
4. **Sprachmodell** — optional: aus, lokal oder OpenAI-kompatibler Endpunkt
   (Abschnitt „Sprachmodell (LLM) einrichten").
5. **Suche** — Embedding-Modell für die semantische Suche (Abschnitt „Suche").
6. **Zugang** — optional ein Administratorkonto anlegen und damit die
   Nutzerverwaltung einschalten (Abschnitt „Nutzer & Sichtbarkeit"). Wird der
   Schritt übersprungen, bleibt Verba ungesichert — das ist nur für die
   lokale Nutzung auf diesem Rechner sinnvoll.

Jeder Schritt lässt sich mit **Schritt überspringen** auslassen; dann gilt die
Standardeinstellung, die später jederzeit in den Einstellungen änderbar ist.
**Später einrichten** verlässt die Einrichtung ganz — der Hinweis, dass sie
noch nicht abgeschlossen ist, bleibt dann bestehen. Solange die Ersteinrichtung
läuft, sind die Navigationstabs ausgeblendet; sie erscheinen, sobald du die
Einrichtung abschließt oder verlässt. Die Einrichtung lässt sich später unter
**Einstellungen** erneut aufrufen.

Schlägt eine Installation fehl, bleiben die bereits fertigen Komponenten
erhalten; ein Neustart von Verba und ein erneuter Versuch räumen nur das
tatsächlich beschädigte Paket auf.

## Transkripte {#transcripts}

Jedes Transkript bekommt einen eigenen **Workspace-Ordner** auf der Festplatte mit
`audio/` (importierte Kopien), `transcripts/` (JSON-Transkripte) und `exports/`.
Alle diese Ordner liegen im **Workspace-Verzeichnis** aus den Einstellungen
(Standard: `workspaces` neben der Anwendung bzw. im Datenverzeichnis der
Installation). Dort sind **absolute Pfade** erwünscht, auch auf Netz- oder
Wechsellaufwerken (`M:\Transkripte`); `~` und `%USERPROFILE%` werden aufgelöst,
und ein Pfad mit Anführungszeichen (wie ihn der Windows-Explorer kopiert) wird
akzeptiert. Ein relativer Pfad wird sofort in einen absoluten umgewandelt und
in den Einstellungen so angezeigt.

Änderst du das Verzeichnis später, **wandern alle vorhandenen
Transkript-Ordner mit** — ein Hintergrund-Job verschiebt sie (auf demselben
Laufwerk in Sekunden, laufwerksübergreifend dauert es so lange wie das
Kopieren) und zieht die Verweise in der Datenbank nach. Existiert im
Zielverzeichnis schon ein Ordner mit gleichem Namen, wird der Wechsel abgelehnt
und nichts verschoben; benenne oder verschiebe den fremden Ordner erst
selbst.

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

## Transkripttypen {#types}

Sechs Standardtypen werden mitgeliefert: **Lied**, **Interview/Dialog**,
**Rede**,  **Protokoll** (Gesprächsprotokoll mit
Zusammenfassung und To-dos), **Gedicht** und **Rollenspiel**.

Jeder Typ legt eine **Gliederung** fest und trägt **zwei Prompts**, zwischen
denen im Editor ein Dropdown umschaltet.

Die **Gliederung** bestimmt, woraus der PDF-Export aufbaut und wie er ohne KI
gliedert:

- **Absätze** — Fließtext aus dem bereinigten Text (Standard).
- **Strophen** — Zeilenumbrüche bleiben erhalten (Lied, Gedicht).
- **Dialog** — baut auf den Segmenten mit ihren Sprechern auf statt auf dem
  zusammengeführten Text, jeder Beitrag mit Sprechernamen (Interview).
- **Drehbuch** — wie Dialog, Rollennamen zusätzlich in Großbuchstaben
  (Rollenspiel, Theaterstück).

Damit kann auch ein selbst angelegter Typ auf die Sprecher-Segmente
zugreifen — das war vorher fest an die Standardtypen gebunden.

Die beiden Prompts:

- **Bereinigungsprompt** — sagt der KI, wie das Transkript selbst aufbereitet
  wird (Absätze, Sprecher, Füllwörter, Strophen …).
- **Ausgabeformat-Prompt** — sagt der KI, wie der aufbereitete Text für den
  **PDF-Export** in Blöcke gegliedert wird: Absätze, Überschriften, Strophen,
  Dialogbeiträge, Listen (z. B. Beschlüsse und To-dos) und Trenner. Die
  Standardtypen bringen dafür einen passenden Prompt mit — beim Lied etwa
  Strophenblöcke, beim Protokoll Listen für Beschlüsse und To-dos.

Bleibt der Ausgabeformat-Prompt leer, greift der Standardprompt; er steht dann
als Platzhalter im Feld, und „Standard einsetzen" holt ihn zum Bearbeiten
zurück. Ein **neuer Typ** ist damit schon vorbelegt, sodass er nur noch
angepasst werden muss. Kann die KI die Vorgabe nicht auswerten, fällt der
Export auf die regelbasierte Gliederung zurück — ein PDF entsteht immer.

Typen werden im eigenen Tab **Typen** (Hauptnavigation) verwaltet: die Liste
zur Auswahl und daneben der Editor für Name, Gliederung und Prompts — auf dem
Smartphone nacheinander als Liste und Detailansicht. Der **+**-Knopf legt
einen neuen Typ an; auch Standardtypen lassen sich bearbeiten und löschen.
„Standardtypen wiederherstellen" bringt gelöschte oder veränderte Standards
zurück (beide Prompts).

## Audio importieren {#import}

Die Aktionskarte im Transkript gliedert den Ablauf in drei Reiter:
**1. Audio importieren → 2. Transkribieren & aufbereiten → 3. Exportieren.**
Ein Tipp auf einen Reiter zeigt genau die Aktionen dieses Schritts; sobald
Dateien vorhanden sind, ist Schritt 2 vorausgewählt.

Drei Wege, alle gleichwertig:

1. **Hochladen** — Dateiauswahl über den Knopf „Dateien hochladen"
2. **Vom Server importieren** — Ordner des Rechners/Servers durchsuchen;
   ein ganzer Ordner importiert alle enthaltenen Audiodateien (auch verschachtelt)
3. **Drag & Drop** — Dateien oder ganze Ordner in den gestrichelten Bereich
   im Schritt „Audio importieren" ziehen; nur dort nimmt Verba Dateien an

Unterstützte Formate: mp3, wav, m4a, flac, ogg, opus, aac, wma, webm, mp4.
Importieren kopiert immer — die Originaldateien bleiben unangetastet.

Beim Hochladen (auch per Drag & Drop) zeigt eine Fortschrittskarte, welche
Datei gerade übertragen wird („Datei 2 von 7“), wie weit die gesamte
Auswahl gediehen ist und wann der Server die Datei speichert.

## Transkribieren {#transcribe}

- **Einzelne Datei:** Mikrofon-Symbol in der Dateizeile (fertige Dateien
  zeigen stattdessen ein Wiederholen-Symbol für einen erneuten Lauf)
- **Alles:** „Alle transkribieren" in Schritt 2 der Aktionskarte
- **Erweitert (aufklappbar):** Whisper-Modell und Aufnahmesprache nur für diesen
  Lauf ändern — die gespeicherten Einstellungen bleiben unberührt
- Fortschritt erscheint live pro Datei; laufende Aufträge sind abbrechbar
- Tipp: Die Aufnahmesprache fest einzustellen (statt automatischer Erkennung)
  verbessert das Ergebnis deutlich

**Wo der Fortschritt zu sehen ist.** Jeder Schritt meldet, an welcher Datei er
arbeitet und wie weit er ist:

- **In der Dateizeile** (Transkript-Ansicht): Balken und Text, z. B.
  `lied.mp3: 01:23` beim Transkribieren (Prozent = Position in der Aufnahme),
  `Bereinigung 2/5` und `Übersetzung 1/3` bei der KI-Aufbereitung,
  `Warteschlange: Position 3` beim Warten. Auch „Alle transkribieren" und
  „KI-Aufbereitung (alle)" legen einen Auftrag **pro Datei** an — jede Zeile
  hat also ihren eigenen Balken.
- **Oben in der Kopfzeile**: eine Zusammenfassung, die auch beim Wechsel der
  Ansicht mitläuft — `KI-Aufbereitung — lied.mp3: Bereinigung 2/5 · 40 %`.
  Laufen zwei Aufträge parallel (Transkription und Aufbereitung), stehen beide
  dort, weitere als `+2 weitere`.
- **In der Transkript-Liste**: ein Hinweis `3 aktiv` am Transkript, solange
  dort etwas läuft.
- **Als eigene Karte**: Aufträge, die zu keiner einzelnen Datei gehören —
  PDF-Export eines ganzen Transkripts, Neuaufbau des Suchindex
  (Einstellungen → Suche), Verschieben der Arbeitsbereiche
  (Einstellungen → Speicherorte).
- **Im Editor**: eigene Balken für „Abschnitt neu transkribieren" und für die
  KI-Aufbereitung der offenen Datei.

**Warteschlange:** Alle Aufträge laufen über eine zentrale Warteschlange, damit
die Hardware nie überlastet wird — auch wenn mehrere Personen gleichzeitig
arbeiten. Wartende Dateien zeigen ihre Position an; kleine Aufträge (Abschnitt
neu transkribieren, Audio-Schnitt) werden bevorzugt eingeschoben, und die
Reihenfolge bleibt fair pro Nutzer.

## KI-Aufbereitung (Bereinigung & Übersetzung) {#ai}

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
  sauber arbeiten — eine Zwei-Stunden-Aufnahme sind rund 17 Anfragen, deren
  Ergebnisse Verba wieder zusammensetzt
- **Gekürzt wird nichts.** Verba gibt dem Modell keine Obergrenze für die
  Antwortlänge mit; es antwortet, soweit sein Kontextfenster reicht. Bricht eine
  Antwort trotzdem mitten im Text ab, halbiert Verba den Abschnitt und fragt
  erneut, statt ein verkürztes Transkript zu speichern. Erst wenn selbst ein
  kurzer Abschnitt nicht mehr passt, endet der Schritt mit einem Fehler
- Ergebnisse erscheinen als Reiter im KI-Dialog und im Editor und werden
  zusätzlich als Markdown-Dateien im Workspace unter `transcripts/` abgelegt

**Was gerade läuft.** Der Dialog schließt sich beim Start — der Fortschritt steht
danach in der Dateizeile und nennt den Schritt (z. B. „KI-Aufbereitung ·
Bereinigung 2/5"). Abgeschlossene Schritte markiert die Zeile mit **bereinigt**
bzw. **übersetzt**; daran ist zu sehen, ob eine Datei die Aufbereitung schon
hinter sich hat. Ein zweiter Klick stellt denselben Schritt nicht doppelt in die
Warteschlange; bei Übersetzungen zählt dabei die Sprache — eine zweite
Zielsprache bekommt ihren eigenen Lauf, und eine Übersetzung startet auch dann,
wenn die Bereinigung derselben Datei gerade noch läuft. Schlägt ein Schritt fehl, erscheint der Grund als Meldung und
bleibt in der Dateizeile stehen — ein leeres Ergebnis wird nie gespeichert, denn
sonst wäre auch jedes daraus gebaute PDF leer. Die Symbole der Dateizeile stehen
in der Reihenfolge des Ablaufs: transkribieren → KI-Aufbereitung → Editor
(Gegenprüfung) → PDF.

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

## Editor & Timeline {#editor}

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
- **Welcher Text am Ende zählt**, steht als Hinweis über den Bereichen: Solange
  keine Bereinigung existiert, entstehen Übersetzung und PDF direkt aus den
  Segmenten. Sobald es eine Bereinigung gibt, ist sie die Grundlage für beides —
  spätere Änderungen an den Segmenten wirken erst, wenn die Bereinigung neu
  erzeugt wird. Ausnahme sind Layouts mit Sprechern (*Dialog*, *Drehbuch*): dort
  baut das PDF immer auf den Segmenten samt Sprecher auf, die Bereinigung dient
  dann nur als Grundlage der Übersetzung.
- **Segmente und KI-Text scrollen gemeinsam** und sind gleich hoch, sodass sich
  eine Passage neben ihrer bereinigten oder übersetzten Fassung lesen lässt. Die
  Zuordnung ist proportional — der bereinigte Text ist ein Fließtext, kein Block
  je Segment.
- Die **Wellenform bleibt beim Scrollen oben stehen** (ab Tablet-Breite), damit
  Abspielen und Auswahl immer erreichbar bleiben.
- **KI-Texte direkt aus dem Editor**: Fehlt ein Text, erstellen ihn
  „Bereinigung erstellen" bzw. „Übersetzung erstellen" (mit Sprachwahl);
  existiert er schon, erzeugt **„Neu erzeugen"** ihn neu und ersetzt ihn.
  Fortschritt und Fehler stehen unter den Bereichen — schlägt der Schritt fehl,
  bleibt der Grund stehen, statt dass scheinbar nichts passiert.
- In der Sprachauswahl der Übersetzung sind die Sprachen in **„Bereits
  übersetzt"** und **„Noch nicht übersetzt"** gruppiert; das Umschalten zeigt
  sofort die jeweilige Fassung.
- **Datei wechseln und exportieren ohne Umweg**: Oben wechselt eine Auswahlliste
  zu einer anderen Datei desselben Transkripts, daneben startet das PDF-Symbol
  den **Export** direkt aus dem Editor.
- **Text und Sprecher** direkt in den Segmentzeilen bearbeiten — Änderungen werden
  automatisch gespeichert („Gespeichert"-Anzeige), **Rückgängig** hebt die letzten
  Änderungen schrittweise auf
- **Auswahl** durch Ziehen auf der Wellenform, dann:
  - **Auswahl neu transkribieren** — nur dieser Abschnitt wird neu erkannt und
    ersetzt exakt die betroffenen Segmente
  - **Auf Auswahl kürzen / Auswahl entfernen** — Audio-Schnitt per ffmpeg;
    das Ergebnis entsteht als *neue* Datei im Transkript, das Original bleibt erhalten
- Im Bereich **PDF-Header** im Editor lassen sich pro Datei ein Titel, ein Zusatz
  und ein Feld für Ort/Datum bearbeiten. Die Kopfzeile lautet dann
  `Titel (Zusatz)` links und Ort/Datum rechts: der Zusatz steht in Klammern
  direkt hinter dem Titel, mit einem Leerzeichen dazwischen. Titel und
  Aufnahmedatum werden automatisch vorgeschlagen. Leere Felder hinterlassen
  keine Spur — kein leeres Klammerpaar, und bei allen drei leeren Feldern gar
  keine Kopfzeile.
- Das rechte Feld ist **freier Text** und nimmt auch einen Ort auf, etwa
  `München, 28.01.1933`. Ein Datum in der Form `JJJJ-MM-TT` wird beim Export
  als `TT.MM.JJJJ` ausgegeben — auch mitten im Text; alles andere bleibt
  unverändert stehen.
- Dateinamen können optional als `Datum_Dateisprache_Zielsprache_Titel_Zusatz` aufgebaut
  sein. Dadurch werden Sprache, optionale Übersetzung sowie die Headerfelder automatisch
  vorbelegt.

## Whisper-Modelle {#whisper}

Im Bereich **Einstellungen → Transkription** zeigt **eine Liste** alle Modelle
mit ihrem Status: Installierte tragen die Markierung „installiert" und lassen
sich direkt löschen, fehlende werden per Klick heruntergeladen. Faustregel:
`small` ist ein guter Start; `large-v3` liefert die beste Qualität, braucht
aber deutlich mehr Leistung. Eigene CTranslate2-Modelle können als Ordner ins
Modellverzeichnis gelegt werden (Unterordner werden erkannt) und erscheinen in
derselben Liste.

**Passt das Modell auf diesen Rechner?** Jede Zeile trägt neben dem Status ein
Urteil für **dieses** System — Whisper läuft immer lokal, gerechnet wird also
mit dem eigenen RAM und dem eigenen Grafikspeicher:

- **geeignet** (grün) — genug Speicher frei, das Modell läuft ohne Klimmzüge
- **eingeschränkt** (orange) — es läuft, aber mit Einschränkung: entweder ist
  der Speicher fast voll, oder das Modell passt nicht in den Grafikspeicher und
  läuft deshalb auf der CPU (deutlich langsamer)
- **zu groß** (rot) — dieses System hat den Speicher nicht; der Hinweis nennt
  gleich das größte Modell, das hier passt

Der Mauszeiger über dem Urteil zeigt den ganzen Satz mit den Zahlen (etwa
„braucht ca. 2,3 GB, frei sind 3,9 GB VRAM"). Über der Liste steht dieselbe
Empfehlung noch einmal für den Rechner als Ganzes; im Einrichtungsassistenten
(Schritt 3) erscheint sie direkt unter dem Modellfeld.

**Wenn der Speicher trotzdem nicht reicht.** Verba stürzt dabei nicht ab:

- Ein Modell, das nirgends hineinpasst, wird **vor** dem Laden abgelehnt — der
  Auftrag schlägt mit einer Meldung fehl, die Anwendung läuft weiter.
- Passt es nicht in den Grafikspeicher, wird der GPU-Versuch übersprungen und
  direkt auf der CPU geladen.
- Läuft der Grafikspeicher erst mitten in der Transkription voll, wechselt
  Verba auf die CPU und schreibt das in die Fortschrittszeile.
- Ist der Arbeitsspeicher voll, endet der Auftrag mit einer Meldung, die die
  freien und die benötigten Gigabyte nennt.

**Ein eigenes Modellverzeichnis** (z. B. eine bestehende Sammlung unter
`M:\Modelle\whisper`) wird unter **Einstellungen → Transkription** eingetragen
und greift sofort, ohne Neustart: Verba liest das Verzeichnis bei jedem Aufruf
neu von der Platte.

- Jeder Unterordner mit einer `model.bin` wird gefunden — auch verschachtelt —
  und erscheint als Modell mit seinem Ordnernamen.
- Heißt ein Ordner genau wie ein Standardmodell (`large-v3`), gilt dieses
  Modell als **installiert** und wird nicht erneut heruntergeladen. Dasselbe
  gilt für den HuggingFace-Cache (`models--Systran--faster-whisper-…`), sofern
  der Download vollständig ist.
- Heißt der Ordner anders (`faster-whisper-large-v3`, `eigene/mein-finetune`),
  erscheint er als eigenes Modell in der Liste und ist auswählbar — nur nicht
  als Standardmodell markiert.
- Ordner ohne `model.bin` und abgebrochene Downloads werden ignoriert.
- Ein Pfad, den es noch nicht gibt, wird angelegt. Liegt er auf einem
  Netzlaufwerk, das gerade nicht verbunden ist, bleibt die Liste leer.

## Sprachmodell (LLM) einrichten {#llm}

Unter **Einstellungen → KI-Aufbereitung (LLM)** wird mit einem Umschalter genau
**ein** Weg gewählt — nur dessen Felder sind sichtbar:

- **Aus** — Bereinigung/Übersetzung deaktiviert, alles andere funktioniert normal
- **Lokal (llama.cpp)** — komplett ohne externe Dienste: Verba zeigt die erkannte
  Hardware (RAM, GPU/VRAM) samt Modellempfehlung; ein Klick installiert llama.cpp,
  ein weiterer lädt das gewünschte Modell. Der lokale LLM-Server startet
  automatisch bei Bedarf.
- **OpenAI-kompatibler Endpunkt** — Base URL, API-Schlüssel und Modellname
  (funktioniert mit OpenAI, Ollama, LM Studio, vLLM u. a.);
  „Verbindung testen" prüft den Endpunkt und listet verfügbare Modelle.

**Endpunkt auf dem eigenen Rechner.** Zeigt die Base URL auf `localhost` bzw.
`127.0.0.1` (typisch für Ollama oder LM Studio), erscheint unter dem Feld eine
**Einschätzung**: wie viel Speicher hier gerade frei ist und welche Modellgröße
damit realistisch ist. Bewusst nur eine Einschätzung und kein Urteil wie bei den
eigenen Modellen — welches Modell dieser Server lädt und ob er die GPU nutzt,
entscheidet er selbst; Verba verwaltet ihn nicht. Bei einer Adresse im Netz oder
in der Cloud steht dort nichts, denn dann ist es fremde Hardware.

**Welche lokalen Modelle?** Verba bringt eine geprüfte Liste mehrsprachiger
Instruct-Modelle mit, sortiert nach dem Hardwarebedarf:

| Modell | Download | Braucht mindestens |
| --- | --- | --- |
| Qwen3 1.7B (Q8) | ca. 2,1 GB | 4 GB RAM/VRAM |
| Qwen3 4B (Q4_K_M) | ca. 2,6 GB | 6 GB RAM/VRAM |
| Gemma 3 4B (Q4_K_M) | ca. 2,4 GB | 6 GB RAM/VRAM |
| Qwen3 8B (Q4_K_M) | ca. 5,2 GB | 10 GB VRAM / 20 GB RAM |
| Gemma 3 12B (Q4_K_M) | ca. 7,0 GB | 14 GB VRAM / 28 GB RAM |

Der **Stern** markiert die Empfehlung für deinen Rechner: Verba prüft VRAM
(bzw. die Hälfte des RAM ohne GPU) und schlägt das größte passende Modell der
Qwen3-Reihe vor. Zusätzlich trägt **jede** Zeile dasselbe Urteil wie bei den
Whisper-Modellen (geeignet / eingeschränkt / zu groß) — es gilt nur für lokale
Modelle, ein OpenAI-kompatibler Endpunkt läuft auf fremder Hardware und wird
deshalb nicht bewertet. Passt ein Modell nicht in den Grafikspeicher, bleibt es
im RAM statt den Server beim Start abzuschießen; passt es nirgends, startet der
Server gar nicht erst und sagt, woran es liegt. Entschieden wird aber nichts
automatisch — du lädst das Modell selbst und kannst jederzeit ein anderes
wählen; im Feld **Lokales Modell** steht, welches der Server benutzt.

**Modelle mit Reasoning.** „Denkende" Modelle (Qwen3, DeepSeek-R1 u. a.) stellen
ihre Überlegungen vor die Antwort — Verba schneidet sie weg. Liefert ein Modell
ausschließlich Überlegungen, oder bricht sein Token-Budget die Antwort ab, bevor
sie begonnen hat, endet der Schritt mit genau dieser Begründung statt mit einem
leeren Text. In LM Studio & Co. deshalb am besten den Denkmodus abschalten oder
ein Modell ohne Reasoning wählen. Fehlermeldungen des Endpunkts (etwa „model not
loaded" oder eine überschrittene Kontextlänge) zeigt Verba im Wortlaut an —
lädt der Server das Modell erst beim ersten Aufruf (LM Studio tut das), kann
dieser Aufruf deutlich länger dauern als die folgenden.

**Eigene Modelle und eigenes Verzeichnis.** Unter **GGUF-Verzeichnis** lässt
sich der Ordner setzen, in dem die Modelle liegen (z. B. `F:\Models\llm`).
Jede `.gguf`-Datei darin steht in der Auswahl und wird **direkt von dort
geladen** — nichts wird kopiert und nichts erneut heruntergeladen. So lässt
sich auch ein Modell verwenden, das gar nicht in der Liste steht: Datei in den
Ordner legen, unter **Lokales Modell** auswählen, fertig.

### Reasoning (Denkmodus)

Manche Sprachmodelle „denken" vor der Antwort — sichtbar an `<think>`-Blöcken
oder daran, dass die Antwort lange auf sich warten lässt. Für Verba bringt das
nichts: Bereinigung, Übersetzung und PDF-Strukturierung sind Umformungen mit
einer klaren Anweisung, keine Aufgaben, über die man nachdenken müsste. Kosten
tut es doppelt — Zeit, und das Token-Budget, das die Antwort selbst braucht.
Genau daher kommt die Meldung „Das Modell hat sein Token-Budget aufgebraucht,
bevor eine Antwort begann".

Unter Einstellungen → KI-Aufbereitung steht dafür **Reasoning**:

- **Aus** (Standard) — Verba bittet das Modell ausdrücklich, nicht
  nachzudenken. Spürbar schneller und die zuverlässigste Einstellung.
- **Wenig** — ein bisschen Nachdenken bleibt erlaubt. Sinnvoll, wenn Sie die
  KI-Antworten in Suche und Hilfe stärker gewichten als die Geschwindigkeit
  der Aufbereitung.
- **Modell entscheidet** — Verba mischt sich nicht ein (Verhalten vor dieser
  Einstellung).

Die Einstellung gilt für alles: Aufbereitung, PDF-Strukturierung, KI-Antworten
der Suche und Fragen zur Hilfe. Technisch schickt Verba `reasoning_effort` und
— für Vorlagen, die nur dort nachsehen — `chat_template_kwargs`. Endpunkte, die
diese Felder nicht kennen (etwa die OpenAI-API selbst), lehnen sie einmal ab;
Verba merkt sich das und lässt sie danach weg. Modelle ohne Denkmodus ignorieren
die Angabe ohnehin.

## PDF-Export {#pdf}

Fertig transkribierte Dateien lassen sich als PDF exportieren — über das
PDF-Symbol in der Dateizeile oder in Schritt 3 der Aktionskarte mit
**PDF-Export (alle)** für das ganze
Transkript. Im Dialog wählst du die Textfassung: Original (bereinigter Text,
sonst das rohe Transkript) oder eine vorhandene Übersetzung.

Für Übersetzungen gibt es **zwei Wege**, im Dialog direkt unter „Original"
wählbar:

- **Eine Fassung pro PDF** — du wählst Original oder eine bestimmte Sprache.
  Der Dateiname trägt die Sprache als Zusatz (`lied.pdf`, `lied.en.pdf`,
  `lied.ru.pdf`), sodass alle Fassungen nebeneinander im Ordner `exports/`
  liegen.
- **„Original + alle Übersetzungen (ein PDF)"** — Original und jede hinterlegte
  Übersetzung landen in **einem** Dokument, untereinander, jeweils durch eine
  zentrierte Zeile `---` getrennt. Die Datei heißt `lied.all.pdf` und
  überschreibt damit keinen Einzelexport. Beim Sammel-Export gilt das je Datei:
  Kopfzeile und Original, dann `---` und die Übersetzungen, danach die nächste
  Datei.

Die angehängten Fassungen wiederholen die **Kopfzeile nicht** — sie wäre
identisch, denn Titel, Zusatz und Ort/Datum sind Metadaten der Datei und werden
nicht mitübersetzt. So markiert eine Kopfzeile eine neue Datei und `---` einen
Sprachwechsel. Wählst du eine einzelne Sprache, für die noch keine Übersetzung
hinterlegt ist, schlägt der Job mit einem Hinweis fehl statt still das Original
zu exportieren; im kombinierten Modus werden einfach nur die vorhandenen
Übersetzungen angehängt.

Der Export läuft zweistufig: Mit konfiguriertem LLM strukturiert die KI den
Text passend zum Transkripttyp (Strophen, Sprecherrollen, Protokoll mit
Zusammenfassung und To-dos); ohne LLM entsteht die Struktur regelbasiert —
der Export funktioniert immer. Das Layout richtet sich nach dem Typ, etwa
unsichtbare Trenner und zusätzlicher Leerraum oder ein
Skriptlayout beim Rollenspiel. Ohne Typ entsteht ein schlichtes Text-PDF.

Fließtext erscheint im **Blocksatz**. Zeilenumbrüche aus dem Transkript werden
dabei zu Wortabständen zusammengezogen, damit ein Absatz nicht mitten im Satz
umbricht — nur Strophen behalten ihre Zeilen. Liefert das Sprachmodell eine
Antwort, die gar nicht aus dem Transkript stammt (etwa die Rückfrage, es sei
kein Text übergeben worden), wird sie verworfen und regelbasiert exportiert.
Enthält das Transkript überhaupt keinen Text — etwa bei einer Aufnahme ohne
Sprache —, schlägt der Export mit einem Hinweis fehl, statt ein PDF zu
erzeugen, das nur die Kopfzeile enthält.

**PDF-Export (alle)** erzeugt ein Sammel-PDF: Jede Datei folgt als eigener
Abschnitt, nur durch Abstand getrennt — ohne Inhaltsverzeichnis und ohne
zusätzliche Titel. Fertige PDFs erscheinen in der Karte **Exporte (PDF)** zum
Herunterladen oder Löschen; im Workspace liegen sie unter `exports/`.

## Suche {#search}

Der Tab **Suche** durchsucht alle Transkripte gleichzeitig — semantisch (die
Bedeutung zählt; deutsche Fragen finden auch englische oder russische Inhalte)
und per Volltext (Eigennamen und seltene Begriffe treffen exakt). Unter
**Filter** lässt sich die Suche auf ein Transkript, einen Typ, Sprache,
Sprecher und einen Zeitraum einschränken.

**Die Trefferliste.** Jede Datei steht genau einmal in der Liste, darunter
alle ihre Treffer in zeitlicher Reihenfolge — jeder mit Zeitstempel, ein Klick
öffnet den Editor genau an dieser Stelle, das Audio startet dort. Von der
Textpassage stehen nur die Fundstellen da: ein bis drei Zeilen um jede
Fundstelle, die Suchbegriffe darin farbig markiert, alles dazwischen mit „…"
ausgelassen. Leert man das Suchfeld, verschwindet die Liste mit der Frage.

**Auch die Kopfzeile wird durchsucht.** Name, Datum und Zusatzhinweis stehen
im Kopf einer Datei, nicht im gesprochenen Text — deshalb sucht Verba dort
mit: Titel, die drei Kopfzeilenfelder, Aufnahmedatum und Dateiname. Ein
solcher Treffer erscheint mit der Marke **Kopfzeile** über den Textstellen
derselben Datei. Gesucht wird wörtlich und mit allen Suchbegriffen zugleich,
damit „Meier 2024" genau diese Datei findet und nicht jede, in der irgendwo
eine 2024 vorkommt; das Datum darf deutsch geschrieben sein („12.05.2024").

Mit konfiguriertem LLM steht **„KI-Antwort"** direkt neben **Suchen** und
erzeugt eine Antwort, die jede Aussage mit nummerierten Quellen belegt. Die
Quellenliste nennt nur Nummer, Transkript, Datei und Stelle — die Passage
selbst steht in der Antwort darüber — und ist klickbar wie ein Treffer. Die KI
antwortet ausschließlich aus den gefundenen Passagen; gibt es keine, sagt sie
das ehrlich statt zu raten.

Neue Transkriptionen und Segment-Änderungen werden automatisch indiziert,
gelöschte Dateien sofort aus dem Index entfernt. Unter **Einstellungen →
Suche** stehen der Index-Status, das Embedding-Modell und ein Knopf für den
manuellen Neuaufbau. Die Suchkomponenten installiert die Einrichtung
(Feature-Gruppe „Semantische Suche"). Fehlen sie noch, bleibt in Schritt 1 der
Einrichtung **Einrichtung starten** anklickbar, auch wenn sonst alles
installiert ist.

**Embedding-Modell.** Zur Auswahl steht eine feste Liste geprüfter Modelle —
alle mehrsprachig (deutsche Fragen finden englische und russische Inhalte) und
CPU-tauglich:

| Modell | Größe | Sprachen | Charakter |
| --- | --- | --- | --- |
| MiniLM multilingual (Standard) | ca. 0,5 GB | 50 | schnell |
| Multilingual E5 small | ca. 0,5 GB | 100 | ausgewogen, etwas genauer |
| mpnet multilingual | ca. 1,0 GB | 50 | gründlich, langsamer |
| BGE-M3 | ca. 2,3 GB | 100 | beste Qualität, spürbar langsamer |

**Passt das Modell auf diesen Rechner?** Wie bei den Whisper-Modellen trägt
jeder Eintrag ein Urteil für dieses System (geeignet / eingeschränkt / zu groß);
unter der Auswahl steht der ganze Satz mit den Zahlen. Gerechnet wird nur mit
dem **Arbeitsspeicher** — die Suche rechnet immer auf der CPU, der
Grafikspeicher spielt hier keine Rolle. Passt das gewählte Modell nicht, nennt
die Meldung ein passendes; und ein Modell, das nicht hineinpasst, wird vor dem
Laden abgelehnt statt den Indexlauf mitten im Speicher scheitern zu lassen.

**BGE-M3** ist die Empfehlung, wenn Qualität vor Geschwindigkeit geht — es ist
das einzige Modell der Liste, bei dem Download (ca. 2,3 GB) und Rechenzeit auf
der CPU auffallen; seine 1024 Dimensionen machen auch den Index größer. Auf
schwächeren Rechnern oder bei sehr vielen Transkripten bleibt der Standard die
bessere Wahl.

Das gewählte Modell wird beim ersten Gebrauch automatisch heruntergeladen —
dafür braucht es einmalig eine Internetverbindung; danach arbeitet die Suche
vollständig offline. Wohin, bestimmt **Modellverzeichnis (Embeddings)**
(Standard: `<Daten>/models/embeddings`). Liegt das Modell dort schon, wird es
**von dort geladen statt erneut heruntergeladen** — erkannt werden sowohl ein
einfacher Ordner (`bge-m3/`, `BAAI_bge-m3/`) als auch ein verschobener
HuggingFace-Cache (`models--BAAI--bge-m3/snapshots/…`). In der Auswahlliste
steht bei solchen Modellen „lokal vorhanden". Ein Wechsel
macht alle gespeicherten Vektoren ungültig und startet deshalb automatisch
einen kompletten Neuindex. Steht im Status „Index stammt von einem anderen
Modell", genügt **Index neu aufbauen**.

## Einstellungen {#settings}

Die Einstellungen sind in Bereiche gegliedert: Auf dem Smartphone erscheint —
wie in einer nativen App — zuerst eine Liste der Bereiche; ein Tipp öffnet den
Bereich als eigene Seite („Alle Einstellungen" führt zurück). Auf dem Desktop
steht die Bereichsliste als Seitenleiste neben dem gewählten Bereich.

- **Oberfläche:** Sprache (Deutsch, Englisch, Russisch), Dokumentation —
  das Handbuch erscheint dort in Abschnitten mit Symbol, jeder Abschnitt lässt
  sich auf- und zuklappen. Mit konfiguriertem Sprachmodell steht darüber
  **Frage zur Hilfe**: eine Frage eingeben, und die KI antwortet
  ausschließlich aus diesem Handbuch. Die Antwort wird formatiert dargestellt
  (Absätze, Listen, Code) wie das Handbuch selbst. Jede Frage beginnt neu
  (kein Chat), und unter der Antwort steht, auf welchen Abschnitten sie
  beruht. Verba schickt
  dabei nur die zur Frage passenden Abschnitte an das Modell; passt selbst das
  nicht in dessen Kontext, wird die Auswahl automatisch verkleinert statt eine
  Fehlermeldung zu zeigen. Ohne Sprachmodell gibt es das Eingabefeld nicht.
- **Transkription:** Standard-Modell, Modellverzeichnis, Gerät (GPU/CPU),
  Rechengenauigkeit, Aufnahmesprache — inklusive der Whisper-Modellverwaltung
  (herunterladen/löschen) im selben Bereich
- **KI-Aufbereitung (LLM):** Aus / Lokal / Endpunkt, GGUF-Verzeichnis
  (Abschnitt „Sprachmodell (LLM) einrichten")
- **Speicherorte & Protokolle:** Workspace-Verzeichnis, Server-Port, Log-Level
  und Aufbewahrungsdauer (ältere Logs werden automatisch gelöscht)
- **Suche:** Index-Status, Embedding-Modell (Auswahlliste),
  Modellverzeichnis, Index neu aufbauen
- **API:** Schlüssel für die öffentliche Transkriptions-API (Abschnitt
  „Öffentliche API")
- **Mein Konto:** eigenes Passwort ändern, eigenes Konto löschen und —
  als Administrator — der Einstieg in die Nutzerverwaltung (Abschnitt
  „Nutzer & Sichtbarkeit“)
- **System:** Informationen über den Rechner, auf dem Verba läuft — CPU
  (Modell und Kerne), Arbeitsspeicher (frei/gesamt), Grafikkarte samt VRAM,
  ffmpeg-Status — sowie die App-Version

## Nutzer & Sichtbarkeit {#security}

Standardmäßig ist Verba **ungesichert**: Wer die Adresse erreicht, sieht alles
und darf alles. Für die lokale Nutzung auf dem eigenen Rechner ist das genau
richtig. Sobald Verba auf einem Server läuft oder mehrere Personen damit
arbeiten, schalten Sie die **Nutzerverwaltung** ein.

### Einschalten

Bei der Ersteinrichtung im Schritt **Zugang**, später jederzeit unter
Einstellungen → Mein Konto → **Nutzerverwaltung öffnen**. Sie legen dort ein
Administratorkonto an; damit ist die Anwendung sofort geschützt und nur noch
nach Anmeldung erreichbar.

Beim Einschalten geht nichts verloren: Alle vorhandenen Transkripte bleiben
unverändert, bleiben **öffentlich** — also für alle angemeldeten Nutzer
sichtbar — und werden dem ersten Administrator als Eigentümer zugeordnet. Er
kann sie anschließend anderen zuordnen oder ihre Sichtbarkeit ändern.

Überspringen Sie den Schritt, wird kein Konto angelegt und Verba bleibt offen.
Die Anwendung sagt das an dieser Stelle auch deutlich.

### Rollen

- **Administrator:** verwaltet Nutzer, Einstellungen, Whisper- und
  Sprachmodelle, Transkripttypen, Suchindex und API-Schlüssel. Sieht und
  bearbeitet jedes Transkript.
- **Nutzer:** arbeitet mit den eigenen und den für ihn freigegebenen
  Transkripten, durchsucht sie, exportiert sie. In den Einstellungen bleiben
  ihm die Oberflächensprache, die Dokumentation und das eigene Konto.

Der erste Nutzer ist immer Administrator. Das **letzte** Administratorkonto
lässt sich weder löschen noch zum normalen Nutzer machen — sonst könnte
niemand mehr Nutzer oder Einstellungen verwalten.

### Konten anlegen

Es gibt keine Selbstregistrierung: Konten legt ein Administrator unter
Einstellungen → Nutzerverwaltung an, mit einem Startpasswort. Beim ersten
Anmelden muss der Nutzer ein eigenes Passwort vergeben — bis dahin kommt er
nicht weiter. Setzt ein Administrator ein Passwort zurück, gilt dasselbe
wieder.

### Sichtbarkeit pro Transkript

Jedes Transkript hat eine von drei Sichtbarkeiten. Sie steht als farbige
Marke auf der Transkriptkachel und lässt sich über das Schloss-Symbol ändern:

- **Privat** — nur der Eigentümer und Administratoren
- **Freigegeben** — zusätzlich die ausdrücklich ausgewählten Personen
- **Öffentlich** — alle angemeldeten Nutzer

**Wer ein Transkript sieht, darf es auch bearbeiten und löschen.** Die einzige
Ausnahme sind die Sichtbarkeit und die Freigabeliste selbst: die ändern nur
der Eigentümer und Administratoren. Sonst könnte ein Kollege ein öffentliches
Transkript auf privat stellen und alle anderen aussperren.

Welche Sichtbarkeit neue Transkripte bekommen, legt ein Administrator unter
Nutzerverwaltung → **Standard-Sichtbarkeit** fest; beim Anlegen lässt sich
davon abweichen.

Die Sichtbarkeit gilt überall, nicht nur in der Übersicht: Die Suche findet
nur, was Sie sehen dürfen, die Statuszeile nennt keine fremden Dateinamen, und
Dateien, Segmente und PDF-Exporte eines fremden privaten Transkripts sind
nicht erreichbar.

### Eigenes Konto

Unter Einstellungen → **Mein Konto** ändern Sie Ihr Passwort (dabei werden
alle anderen angemeldeten Geräte abgemeldet) oder löschen Ihr Konto.

Beim Löschen eines Kontos gilt:

- **Private** Transkripte werden mitsamt Audiodateien gelöscht — sie gehörten
  nur dieser einen Person.
- **Freigegebene und öffentliche** Transkripte bleiben erhalten und gehen an
  den dienstältesten Administrator über. Sie sind Arbeitsmaterial anderer und
  sollen nicht unter deren Händen verschwinden.

Dasselbe passiert, wenn ein Administrator ein fremdes Konto löscht. Die
Anwendung nennt vor dem Löschen die betroffene Anzahl.

### Was sich sonst noch ändert

- Die **öffentliche API** (`/v1`) verlangt bei aktiver Nutzerverwaltung immer
  einen API-Schlüssel — sonst wäre die Anmeldung über diesen Weg umgehbar.
- Die Anmeldung läuft über ein Sitzungs-Cookie. Passwörter werden mit scrypt
  gehasht gespeichert, von Sitzungen nur eine Prüfsumme; im Klartext liegt
  weder das eine noch das andere in der Datenbank.
- **Reverse-Proxy mit TLS-Terminierung** ist der Normalfall und funktioniert:
  Verba liest das Schema des Browsers aus `X-Forwarded-Proto` und markiert
  das Cookie danach als `Secure` — die Verbindung vom Proxy zu Verba darf
  einfaches HTTP bleiben. Der Header wird nur von `127.0.0.1` akzeptiert;
  läuft der Proxy auf einem anderen Rechner, Verba mit
  `FORWARDED_ALLOW_IPS=<Proxy-IP>` starten oder in den Einstellungen
  `auth.cookie_secure` auf `always` setzen.

### Wieder ausschalten — und wieder ein

Unter Nutzerverwaltung → **Nutzerverwaltung deaktivieren**. Danach ist Verba
wieder für jeden offen, der die Adresse erreicht. Was dabei passiert:

- Alle laufenden Anmeldungen enden sofort.
- Die Konten bleiben mit ihren Passwörtern und Rollen erhalten.
- Eigentümer, Sichtbarkeiten und Freigaben bleiben in der Datenbank stehen —
  sie werden nur nicht mehr durchgesetzt. Ein privates Transkript ist also
  wieder für jeden erreichbar, aber es bleibt als privat gespeichert.
- Die öffentliche API (`/v1`) fällt auf ihre alte Regel zurück: offen,
  solange kein API-Schlüssel angelegt ist.

Zum Wiedereinschalten genügt derselbe Knopf — er heißt dann
**Nutzerverwaltung wieder aktivieren** und fragt nach nichts: Es wird kein
zweites Administratorkonto angelegt, alle melden sich mit ihrem bisherigen
Passwort an, und Eigentümer wie Sichtbarkeiten gelten wieder wie vorher.
Transkripte, die währenddessen ohne Nutzerverwaltung angelegt wurden, haben
keinen Eigentümer; sie gehen an den dienstältesten Administrator und bleiben
öffentlich, sperren also niemanden aus.

## Öffentliche API {#api}

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
