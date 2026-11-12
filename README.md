# DiskFormatter

Windows-Tool (Python + tkinter) zum Formatieren von Laufwerken (NTFS, exFAT, FAT32) und zum
sicheren Löschen: vorher jeden Sektor mit Nullen überschreiben, wahlweise nur ein Volume oder den
ganzen Datenträger inklusive Partitionstabelle, z. B. bevor ein USB-Stick oder eine Platte verkauft wird.

**Sicherheitsprinzip:** Das Systemlaufwerk, Boot-Datenträger und der Datenträger mit Windows sind
immer gesperrt. Standardmäßig sind nur Wechseldatenträger/USB auswählbar. Vor jeder Aktion kommt eine
Zusammenfassung, und der Laufwerksbuchstabe bzw. die Datenträgernummer muss zur Bestätigung
eingetippt werden.

## Starten

```bash
python DiskFormatter.pyw
```

oder Doppelklick auf `DiskFormatter.pyw`. Das Programm braucht Administratorrechte und startet sich
dafür über die UAC-Abfrage neu. Lehnt man ab, läuft es ohne Adminrechte nur als Ansicht.
Nur Python 3.10+ (getestet mit 3.13), keine weiteren Pakete.

## Funktionen

- **Laufwerksliste** (`Get-Disk`, `Get-Partition`, `Get-Volume`): Datenträger mit ihren Volumes,
  Buchstabe, Name, Größe, Dateisystem, Bustyp (USB/SATA/NVMe …), Wechseldatenträger ja/nein,
  System ja/nein und der Grund, falls gesperrt.
- **Formatieren:** Dateisystem, Volumenname, Schnell- oder vollständige Formatierung.
- **Volume mit Nullen füllen:** `format X: /FS:… /P:0` überschreibt jeden Sektor des Volumes mit Nullen.
  Partitionstabelle und andere Volumes bleiben erhalten.
- **Ganzen Datenträger mit Nullen füllen:** `MSFT_Disk.Clear` mit `ZeroOutEntireDisk` (alle Sektoren
  inkl. Partitionstabelle), danach `Initialize-Disk` (MBR für Sticks unter 2 TB, sonst GPT,
  wählbar), `New-Partition -UseMaximumSize` und `Format-Volume`.
- **Stichprobe** nach dem Nullen: zufällige Sektoren werden gelesen und auf 0 geprüft (Anzahl in der
  Einstellungsdatei `verify_samples`, Standard 256). Beim ganzen Datenträger müssen alle gelesenen
  Blöcke 0 sein, sonst gilt der Vorgang als fehlgeschlagen. Beim Volume zählen nur die frisch
  geschriebenen Dateisystem-Metadaten nicht als 0.
- **Fortschritt und Restzeit**, **Abbrechen** jederzeit möglich. Ein abgebrochenes Laufwerk ist danach
  teilweise genullt und unformatiert und muss neu formatiert werden.

### Ersatzwege (automatisch)

- Windows verweigert `format /P:0` und die vollständige Formatierung auf Datenträgern, die als
  *thin provisioned* gelten. Das betrifft virtuelle Platten (VHD/VHDX) und manche TRIM-fähigen
  USB-SSDs. Dann sperrt DiskFormatter das Volume, hängt es aus, schreibt die Nullen direkt
  (`\\.\X:`, inkl. `FSCTL_ALLOW_EXTENDED_DASD_IO` für die NTFS-Bootsektor-Kopie) und formatiert
  danach schnell.
- Unterstützt ein Datenträger `ZeroOutEntireDisk` nicht, werden zuerst die Partitionen entfernt
  (`Clear-Disk -RemoveData`) und die Nullen direkt auf `\\.\PhysicalDriveN` geschrieben.
- Das Cmdlet `Clear-Disk` hat (anders als die WMI-Methode) keinen Parameter `-ZeroOutEntireDisk`.
  Deshalb ruft DiskFormatter `MSFT_Disk.Clear` direkt als Storage-Job auf. Das liefert auch echte
  Prozentangaben und lässt sich über `RequestStateChange` abbrechen.

## Sperren

| Sperre | Wirkung |
|---|---|
| Datenträger mit `IsSystem`/`IsBoot`, mit System-/EFI-/Boot-Partition | nie auswählbar |
| Datenträger mit `%SystemDrive%` oder dem Windows-Ordner | nie auswählbar |
| Datenträger mit einer Auslagerungsdatei | nie auswählbar |
| Datenträger, von dem DiskFormatter/Python gerade läuft | nie auswählbar |
| Offline, schreibgeschützt, CD/DVD, Netzlaufwerke | nicht auswählbar |
| Interne Platten (kein USB/SD/Wechseldatenträger) | nur nach *Einstellungen → Interne Festplatten freischalten …* (mit Warnung) |

Vor der Ausführung wird alles **neu abgefragt und neu geprüft**. Dabei wird auch verglichen, ob es
noch derselbe Datenträger ist (Kennung, Seriennummer, Größe). Ein in der Zwischenzeit umgesteckter
Stick wird also nicht versehentlich gelöscht.

Einstellungen: `%APPDATA%\DiskFormatter\settings.json`. `allow_internal` lässt sich nur in der GUI
umschalten; die CLI liest den Wert nur.

## Kommandozeile / Claude (ToolHub)

`cli.py` gibt JSON aus; `toolhub.json` beschreibt die Befehle für ToolHub/MCP.

```bash
python cli.py drives [--selectable]
python cli.py plan --action wipe --target E: --fs exFAT --label STICK
python cli.py format --target E: --fs exFAT --label STICK --confirm-target E: [--dry-run]
python cli.py wipe --target 3 --fs NTFS --style GPT --confirm-target 3 [--dry-run]
```

- `drives`, `plan`: nur lesend.
- `format`, `wipe`: ändernd. `--dry-run` zeigt nur den Plan (inkl. `confirm_target_ok`).
  Ohne `--dry-run` muss `--confirm-target` exakt zum Ziel passen (Buchstabe bzw. Nummer), sonst
  Exit-Code 2. Es gelten dieselben Sperren wie in der GUI. Fehlen Adminrechte, fragt Windows per UAC
  nach. Der Vorgang läuft dann in einem versteckten Admin-Prozess, und das Ergebnis kommt zurück.
- Fehler: Meldung auf stderr, Exit-Code ≠ 0.

## Tests

```bash
python -m unittest discover -s tests
```

prüft Sperren, Bestätigung und Plan mit nachgebauten Laufwerksdaten, ohne echte Laufwerke anzufassen.

```bash
python tests\vhd_integration.py --out ergebnis.json
```

(als Administrator) legt eine 2-GB-Test-VHD an, hängt sie an und testet daran CLI und GUI:
Formatieren, Volume nullen, ganzen Datenträger nullen (beide Wege), Sperren, falsche/fehlende
Bestätigung, `--dry-run` und Abbrechen mitten im Schreiben. Vor jeder Aktion wird geprüft, dass das
Ziel die Test-VHD ist (Bustyp + Dateipfad). Am Ende wird sie getrennt und gelöscht.

**Bekannte Grenze:** Auf der Test-VHD lehnt Windows `format /P:0` immer ab (thin provisioned).
Dort ist deshalb nur der Ersatzweg „direkt schreiben“ getestet, und der Weg über `format.com /P:0`
ist nur auf echten, nicht-thin Laufwerken aktiv. Die vollständige Formatierung per `format.com`
verhält sich auf der VHD genauso.

**Abbrechen beim ganzen Datenträger:** Den `ZeroOutEntireDisk`-Job von Windows bittet DiskFormatter
per `RequestStateChange` um ein Ende. Auf der Test-VHD war er trotzdem fertig, bevor das wirkte. In
dem Fall sagt die Meldung ehrlich „vollständig genullt, ohne Partition“. Auf dem direkten Weg
(Ersatzweg) bricht DiskFormatter nachweislich mitten im Schreiben ab.

> **Achtung:** Dieses Tool verändert Datenträger. Falsch angewendet kann es Daten unwiderruflich löschen. Nutzung auf eigenes Risiko, ohne Gewähr. Systemdatenträger sind gesperrt, trotzdem vorher Backups machen.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
