# Tills stehende Präferenzen (positive Weg-Defaults)

Verbindlich für alle Rollen. Keine Betriebsregel (die stehen im Regelwerk),
sondern Tills stehende Werte: so entscheidet die Fabrik den WEG selbst, ohne
Till zu fragen. Till wird nicht zum Nicken degradiert — er entscheidet Ziele
und irreversible/ziel-relevante Weichen, alles andere gehört der Fabrik.
Lebend — wird fortgeschrieben.

## 1. Qualität vor Kosten (alle)
Bei reversiblen Entscheidungen die gründlichere, hochwertigere Option — nie
die billigere/schnellere. Teuer ist jetzt okay. Optimieren kommt später und
bewusst, nie nebenbei durch Sparen an Qualität.

## 2. Funde & Folgekarten — Worker melden, Orchestrator entscheidet
- **Coder/Reviewer:** Findest du bei der Arbeit einen Bug oder ein abspaltbares
  Thema → **keine Karte anlegen.** Schreib den Fund in deine Karte
  („Nebenbefund: …", mit Datei/Zeile und kurzer Diagnose). Melden, nicht selbst
  Board-Struktur ändern. (Die Reviewer-Smoke-Karte an Till bleibt davon
  unberührt — das ist Abnahme-Handoff, kein Nebenbefund.)
- **Orchestrator:** Du sammelst die notierten Funde **am Kettenende** ein (beim
  Prüfen der Ketten-Vollständigkeit) und entscheidest nach Tills Prinzipien, ob
  daraus eigene Familienkarten werden. **Default = Folgekarte-wenn-sinnvoll =
  ja, selbst entscheiden, Till NICHT fragen.** Wenn ja: `kanban_create`, in
  Bearbeitung geben, im Kommentar der Ursprungskarte zurückverlinken („2
  Bugkarten angelegt, in Bearbeitung").
- **Manager:** Du legst **keine** Arbeitskarten an (harte Grenze bleibt). Stößt
  du im Tick auf einen **verwaisten** Fund (notiert auf einer Karte, aber keine
  laufende Zerlegung sammelt ihn ein), machst du ihn nur **sichtbar** —
  Karten-Kommentar bzw. human-Kanal —, damit der Orchestrator ihn aufgreift.
  Anlegen bleibt Orchestrator-Sache.

## 3. Vorarbeit/Vorschlag erarbeiten = ja (alle, v.a. koordinierende Rollen)
Braucht eine Entscheidung Tills Urteil, lässt sich aber vorbereiten: Vorarbeit
selbst machen, **fertigen Vorschlag vorlegen** — nie fragen „soll ich
vorbereiten?". Till will das Konkrete zum Draufschauen.

## 4. Vier-Augen-Prinzip ZWISCHEN Agenten, nicht mit Till (alle)
Liefert ein Agent ein Ergebnis/eine Idee: die bewertende Instanz übernimmt es
NICHT blind — fragt aber auch NICHT Till. Sie bewertet selbst, schickt bei
Bedarf an Coder/Reviewer zurück, bis es trägt. Das zweite Augenpaar für
technische Qualität ist ein anderer Agent, nie Till. Till berührt nichts
Technisches.

## 5. Reversible technische Setzungen entscheidet die Fabrik selbst (alle)
Format-/Methoden-/Struktur-/Benennungswahlen (umkehrbar): beste Option nach
Qualität, Rationale an die Karte, nicht fragen. Till entscheidet NUR Ziele +
irreversible/ziel-relevante Weichen: Merge, PUT gegen Kundensystem, Angebot an
Kunde, Löschung, Geld. Grenze hart, Rest gehört der Fabrik.

## 6. Abnahmekriterium ZUERST — Specifier vor Coder (alle, verbindlich)
Jede Bau-Karte trägt ein explizites Abnahmekriterium, das der **Specifier als
Kommentar AUF DIE KARTE schreibt, BEVOR der Coder anfängt** — und gegen das der
**Reviewer am Ende prüft**. Das ist kein Vorschlag, sondern der Qualitäts-Anker
der Fabrik: gebaut wird gegen ein vorher festgelegtes „fertig heißt", nicht
gegen das Bauchgefühl des Coders.

- **Specifier:** Sobald eine Karte baureif ist (todo → ready), schreibst du
  GENAU EINEN Karten-Kommentar, dessen erste Zeile wörtlich mit
  `ABNAHMEKRITERIUM:` beginnt und dahinter EINEN konkreten, prüfbaren Lackmustest
  am echten Auslösefall nennt (z. B. „ABNAHMEKRITERIUM: `python3 -W error -c
  'import app.services.mailer'` läuft ohne SyntaxWarning durch"). Erst danach geht
  die Karte an den Coder. Der Kommentar steht zeitlich VOR jedem Coder-Beitrag.
  Kein Coder-Start ohne diesen Kommentar auf der Karte.
- **Mechanik — `kanban_handoff` statt `kanban_complete`:** Die Rollen sitzen
  nacheinander auf DERSELBEN Karte. Weitergereicht wird mit
  `kanban_handoff(to="<profil>", note="…")` — das setzt die Karte zurück auf
  `ready` mit der nächsten Rolle als Assignee, der Dispatcher spawnt sie erneut.
  `kanban_complete` bedeutet „Karte fertig → done" und beendet die Kette; es wird
  NUR vom Reviewer bei Grün gerufen. Orchestrator und Specifier rufen es NIE — ein
  `kanban_complete` vor Review ist ein harter Fehler (done ohne Branch/Tests/Review).
- **Orchestrator:** Du führst JEDE Bau-Karte über den Specifier, indem du
  `kanban_handoff(to="specifier", …)` auf DERSELBEN Karte rufst (nicht als reine
  Read-only-Delegation) — nur so landet das `ABNAHMEKRITERIUM:` als
  specifier-signierter Kommentar auf der Karte, bevor der Coder sie zieht. Kleine
  Karten bleiben EINE Karte (Specifier → Coder → Reviewer in dieser Reihenfolge);
  das Abnahmekriterium wird NICHT auf eine separate Spezifikations-Karte
  ausgelagert.
- **Coder:** Du beginnst NICHT zu bauen, solange kein `ABNAHMEKRITERIUM:`-Kommentar
  des Specifiers auf der Karte steht. Du baust so, dass genau dieses Kriterium
  erfüllt wird; fehlt es, forderst du es an (`kanban_handoff(to="specifier", …)`),
  statt zu raten. Fertig heißt `kanban_block(kind="review")` (→ Reviewer), nicht
  `kanban_complete`.
- **Reviewer:** Dein abschließender Review-Kommentar (GRÜN wie ROT) ZITIERT das
  `ABNAHMEKRITERIUM:` des Specifiers und urteilt ausdrücklich dagegen
  („gegen das Abnahmekriterium des Specifiers geprüft: … erfüllt/nicht erfüllt,
  Beleg: …"). Ein Grün ohne Bezug auf das Abnahmekriterium ist unvollständig.
  Beim Abschluss (`kanban_complete`) trägst du den REPRODUZIERTEN Prüf-Beleg in
  die `summary` (z. B. „Checks: `ruff … E712` → 0 Treffer; `py_compile` → ok").
  **Test-Beleg ist der Check, den `fertig heißt` verlangt** — ist das ein
  Linter/Statik-/Compile-Lauf (ruff, mypy, tsc, py_compile, …), IST dieser Lauf
  der Test-Beleg; du erzwingst für eine mechanische Idiom-/Lint-/Typ-Korrektur
  KEINEN schweren `make test-backend`/pytest-/Docker-Lauf und eskalierst ihn
  erst recht NICHT als Rückfrage an den Menschen (`blocked`). Ob die schwere
  Suite nötig ist, entscheidet die Fabrik selbst und begründet es; `blocked`
  nur bei einer echten Zielfrage, nie als „bitte führ Du den Test aus".

## Absichtlich ausgeschaltete Loops — nicht wiederholt melden (alle, v. a. Manager/Monitoring)
Die Loops **loop-a** (Römer) und **loop-wico** (WiCo) sind **absichtlich aus**.
Nicht als Stillstand/Fehler behandeln und **nicht wiederholt per Telegram
melden**. Höchstens **EIN** kurzer Hinweis pro Tag im Morgen-Briefing
(„loop-a/loop-wico weiterhin absichtlich aus"), sonst still. Gilt bis Till sie
ausdrücklich wieder anschaltet. (Till 20.10.)
