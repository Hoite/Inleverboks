# Changelog

Alle noemenswaardige wijzigingen staan in dit bestand. Het formaat volgt
[Keep a Changelog](https://keepachangelog.com/nl/1.1.0/) en de versies volgen
[Semantic Versioning](https://semver.org/lang/nl/).

## [Unreleased]

## [0.3.1] - 2026-11-25

### Opgelost

- **Migraties op SQLite wisten gegevens.** Een migratie die een tabel opnieuw opbouwt (Alembic
  *batch*) liet de foreign keys aan staan. Het verwijderen van de oude tabel wiste daardoor de
  onderliggende rijen (`ON DELETE CASCADE`) of brak halverwege af en liet `_alembic_tmp_…` achter.
  Getroffen: de migratie van 0.2.0 wiste de verwachte bestanden (en de koppeling met
  sjabloonbestanden) van alle examensjablonen. Die van 0.3.0 wiste de verwachte bestanden,
  klassenlijsten en verlengingen van examens zonder inleveringen, of brak af bij examens met
  inleveringen. Migraties zetten de foreign keys nu uit, ruimen een achtergebleven kopie op en
  controleren daarna alle verwijzingen. PostgreSQL had hier geen last van.

## [0.3.0] - 2026-11-25

### Toegevoegd

- Klassenlijsten zijn voor alle docenten zichtbaar en bruikbaar, met een kolom *Eigenaar* en de
  keuze *Mijn lijsten / Alle lijsten*. Iedere docent kan een eigen kopie maken.
- Een klassenlijst hernoemen en vervangen door een nieuwe CSV (eigenaar en admins). Bestaande
  examens houden hun eigen momentopname.
- Admins: een kolom *Eigenaar* en een filter op eigenaar onder *Alle examens*, en de weergave
  *Alle sjablonen* met de persoonlijke sjablonen van alle docenten. Een admin kan een
  persoonlijk sjabloon van een ander bekijken en kopiëren naar zijn eigen of naar de gedeelde
  sjablonen.
- Een kolom *Eigenaar* onder *Gedeeld met mij* (en, voor admins, onder *Afgeronde examens*).

### Gewijzigd

- De docentpagina's staan nu onder `/examens` in plaats van `/docent`: `/examens/{id}`,
  `/examens/sjablonen`, `/examens/klassenlijsten`, `/examens/gedeeld` enzovoort. Oude
  `/docent/…`-adressen sturen permanent door naar het nieuwe adres (301 voor GET, 308 voor
  formulieren), met behoud van de querystring.
- *Ontvangstbewijs controleren* is een admintaak geworden: een tabblad onder *Admin*
  (`/admin/ontvangstbewijs`). Docenten zien de link niet meer in de kop en krijgen 403.
- Het logo in de kop gaat altijd naar de startpagina (`/`), ook als je bent ingelogd.
- De startpagina kent je sessie: ingelogd zie je de gewone kop en een knop *Naar mijn
  examens*. De uitleg voor studenten blijft staan.
- In de kop staat nu een accountmenu (persoonicoon en je naam) met *Account* en
  *Uitloggen*. Het is gebouwd met `<details>`, werkt zonder JavaScript en sluit met Escape of
  een klik ernaast.
- Een examen heeft een vierde tabblad *Geavanceerd* (`/examens/{id}/geavanceerd`) met de
  acties die je zelden nodig hebt: dupliceren, terug naar concept, concept verwijderen,
  opslaan als sjabloon en eigendom overdragen, plus de bewaartermijn met de bewaren-vlag (en
  definitief verwijderen). De waarschuwing dat bestanden binnenkort worden verwijderd staat
  nog op *Inleveringen* en op het dashboard, met een link naar dit tabblad.
- Een nieuw examen staat standaard op de eerstvolgende werkdag (morgen, of maandag als
  morgen in het weekend valt) van 09:00 tot 17:00 in `APP_TIMEZONE`. Dupliceren en een
  herkansing beginnen ook op de eerstvolgende werkdag om 09:00, met de duur van het origineel.
- In het examenformulier schuift het einde mee als je de start verandert (de duur blijft
  gelijk). Een voorgestelde linknaam met een datum (`ddmmjjjj`, bij sjablonen uit een
  kwalificatie) volgt de startdatum, tot je zelf een linknaam typt. Ook zonder JavaScript
  zet de server de datum in een ongewijzigde voorgestelde linknaam goed. Het voorstel
  gebruikt de geplande datum, niet die van vandaag.
- Het grijze vak met het adres vóór de linknaam is zo breed als de tekst (het invoerveld
  houdt minstens 10rem). Past het niet, dan valt eerst `https://` weg en daarna wordt het
  links afgekapt met een beletselteken. Het volledige adres staat in de tooltip.
- Zoekbare keuzelijsten voor sjabloon en klassenlijst bij *Nieuw examen*, tweede
  beoordelaar en eigendom overdragen: typ een deel van de naam of het e-mailadres (ook
  zonder accenten), kies met de pijltjes en Enter, sluit met Escape. Zonder JavaScript blijft
  het een gewone keuzelijst; de server ziet geen verschil.
- Een examen dat uit een sjabloon is gemaakt, toont de verwachte bestanden alleen ter
  inzage, met een link naar het sjabloon: je wijzigt ze in het sjabloon. Dat geldt bij het
  aanmaken en bij later bewerken, en de server negeert wijzigingen aan de vakjes in het
  formulier (bij een deelexamen tellen alleen de gekozen werkprocessen). Sjabloonbestanden
  uploaden op de instellingenpagina kan nog steeds. Examens zonder sjabloon, het
  sjablonenformulier en herkansingen werken zoals voorheen. Verwijder je het sjabloon, dan
  worden de vakjes weer bewerkbaar. Nieuwe kolom `exam.source_template_id` (migratie
  `d4f2b7a91c35`); bestaande examens krijgen geen koppeling.
- Alleen de eigenaar en admins mogen een klassenlijst wijzigen of verwijderen. Dit staat nu als
  matrix in `policy.py`. Kopiëren, hernoemen, vervangen en verwijderen komen in het auditlog.

### Opgelost

- Het proces-verbaal (en ook het ontvangstbewijs en de auditlog-PDF) liep bij lange namen,
  bestandsnamen en hashes van de pagina af. Tabellen hebben nu een vaste indeling met
  kolombreedtes, en lange woorden en hashes breken af. Een test controleert met de
  layout van WeasyPrint dat niets buiten de pagina valt.
- De teller in de presentatiemodus daalde als de docent een inlevering ongeldig verklaarde. Hij
  telt nu het aantal studenten met een actieve of ongeldige inlevering, één per student.

## [0.2.0] - 2026-11-24

Sjablonen op basis van de kwalificatiestructuur van SBB.

### Toegevoegd

- Kwalificaties (uitstroom, crebo), kerntaken en werkprocessen in de database, per
  dossierversie.
- Het SBB-bestand inlezen (XML *Herziening dossiers*, `dossiers.xml`, of een zip daarmee):
  onder *Admin → Kwalificaties* of met `inleverboks sbb-import BESTAND`. Concepten worden
  overgeslagen; varianten met dezelfde crebo worden samengevoegd.
- Sjablonen uit een kwalificatie: zoeken op crebo of naam (direct tijdens het typen), kerntaken
  kiezen, en per kerntaak een sjabloon met een vakje per werkproces
  (`B1-K1-W1 [naam]`).
- Gedeelde sjablonen, beheerd door de admin en bruikbaar voor elke docent; docenten kunnen er
  een eigen kopie van maken.
- Deelexamens: bij een nieuw examen vink je aan welke werkprocessen meedoen. Naam en linknaam
  worden voorgesteld (`27016 ICT System Engineer – B1-K1 …`, `27016-b1-k1-23112026`).
- Het examen bewaart een momentopname van de kwalificatie; die staat in het proces-verbaal en op
  de examenpagina.
- Codes in de exports: een kolom per werkproces in de CSV, een kolom `werkproces` in het
  manifest, en bestandsnamen in de zip die met de code beginnen.
- Overzicht per werkproces: een kolom per werkproces met een vinkje per student.
- Herkansing in één klik vanuit het overzicht, of voor meerdere studenten tegelijk: een
  nieuw examen met alleen de openstaande werkprocessen en alleen die studenten.
- Een waarschuwing bij een sjabloon waarvan het dossier is vervangen of verlopen, met de
  gewijzigde werkprocessen. Dat werkt ook als SBB de uitstroom bij een herziening een nieuwe
  crebo geeft. In de zoekresultaten staan vervangen en verlopen kwalificaties onderaan, met
  een label.
- `demo-seed` voegt een fictieve kwalificatie (crebo 99999) toe om dit uit te proberen.

### Opgelost

- Zoekvelden hebben dezelfde opmaak als andere invoervelden.

## [0.1.3] - 2026-11-23

### Opgelost

- Bij het dupliceren van een examen met een klassenlijst ging "studenten toestaan die niet op
  de klassenlijst staan" ten onrechte aan (sinds 0.1.1), zodat ook andere studenten konden
  inleveren. De kopie neemt de instelling en de klassenlijst nu weer correct over.

## [0.1.2] - 2026-11-23

### Opgelost

- De QR-code voor tweestapsverificatie viel aan de rand weg: de SVG had een vaste breedte en
  geen `viewBox`, terwijl de pagina hem verkleinde. Hij schaalt nu mee. Alle QR-codes hebben
  een witrand van 4 modules, zoals de QR-standaard voorschrijft.
- De sleutel om tweestapsverificatie handmatig in te stellen wordt in groepjes getoond, maar
  kopiëren geeft nu de sleutel zonder spaties. Een hint legt uit dat een O altijd de letter O
  is (een 0 komt in de sleutel niet voor).

## [0.1.1] - 2026-11-23

Verbeteringen na de eerste bètatest.

### Toegevoegd

- Examensjablonen zijn nu volwaardig te beheren: een overzicht, een detailpagina, zelf een
  sjabloon maken of bewerken (verwachte bestanden, extensies, limieten, toegang) en
  sjabloonbestanden per vakje uploaden. Sjablonen hebben een eigen kleur en markering, zodat ze
  nooit op een examen lijken.
- Bij **Nieuw examen** kies je bovenaan een sjabloon; de naam van het sjabloon wordt alvast
  ingevuld.
- De versie van de app staat in de footer.

### Gewijzigd

- Start, einde en verlengingen hebben een datumkiezer plus een tijdveld in 24-uursnotatie
  (`uu:mm`, ook `9.30` of `0930`) in plaats van `datetime-local`, dat in een Engelstalige
  browser AM/PM toont.
- Zonder klassenlijst staat "Studenten toestaan die niet op de klassenlijst staan" altijd aan
  (uitgegrijsd); de server dwingt dat ook af.
- "Opslaan als sjabloon" opent daarna het nieuwe sjabloon, en legt uit wat er wordt
  overgenomen.
- Sjabloonbestanden gaan bij een examen vanuit een sjabloon mee met hun eigen vakje, ook als
  je vakjes toevoegt of weghaalt.

### Opgelost

- De sectie met tijdverlengingen heette "Extensies" (met "Geen verlengingen."). Die heet nu
  "Verlengingen"; de bestandsextensies staan als "Toegestane extensies" bij de details, en een
  vakje zonder eigen extensies toont "Alle toegestane extensies".
- De sleutel bij het instellen van tweestapsverificatie staat op een eigen regel.

## [0.1.0] - 2026-11-14

### Toegevoegd

- Fundament (fase 1): projectstructuur met uv, configuratie via omgevingsvariabelen,
  `/healthz` en `/readyz`, strikte security headers, een limiet op de request body,
  ondersteuning voor vertrouwde proxy's, JSON-logging, i18n (Nederlands), Alembic, de CLI
  (`migrate`, `version`), een Dockerfile (non-root, read-only root filesystem), compose-bestanden
  en de CI-workflow.
- Kern (fase 2):
  - het volledige datamodel met de eerste migratie;
  - een append-only auditlog met een hash-keten (de database weigert UPDATE en DELETE) en
    `inleverboks audit verify`;
  - de bootstrap van de local admin en `inleverboks create-admin [--reset]`;
  - login met argon2id, server-side sessies, tweestapsverificatie (TOTP) met herstelcodes, en
    rate limiting met oplopende vertraging en blokkade;
  - de whitelist, uitnodigings- en resetlinks, en het (de)activeren en het geven en afnemen van
    adminrechten;
  - adminschermen voor gebruikers, whitelist, instellingen en het systeemlog;
  - de centrale autorisatiematrix, CSRF-bescherming en de S3-opslagbackend.
- Examens (fase 3):
  - aanmaken op één scherm, met verstandige standaardwaarden; bewerken met de oude en nieuwe
    waarde in het auditlog; publiceren, terug naar concept, dupliceren en een concept
    verwijderen;
  - vakjes met verwachte bestandsnamen (wildcard `*`), extensies en sjabloonbestanden
    (SHA-256); de bestandseisen liggen vast na de eerste inlevering;
  - examensjablonen (persoonlijk); inleverlinks als `/{slug}` (eigen naam of gegenereerd,
    met gereserveerde woorden, en nooit hergebruikt); toegangscode; IP-whitelist met
    presets van de admin;
  - QR-code (PNG/SVG), presentatiemodus voor de beamer met aftellen en een teller;
  - klassenlijsten: CSV-import (`;`/`,`, UTF-8 met BOM, Windows-1252, kolommen koppelen,
    preview en foutmeldingen), een persoonlijke bibliotheek en een momentopname per
    examen;
  - verlengingen per student, eigendomsoverdracht door de admin, en de dashboardtabbladen;
  - de publieke statuspagina onder `/{slug}`; htmx meegeleverd met checksum; cache-busting
    voor statische bestanden.
- Studentflow (fase 4):
  - de stappen toegangscode (met rate limiting), studentnummer (met de verkorte naam van de
    klassenlijst en "Ja, dat ben ik") en naam bij niet-vermelde studenten; de IP-whitelist
    met vertrouwde proxy's; geweigerde pogingen per minuut samengevoegd in het auditlog;
  - bestanden kiezen of slepen, automatisch toewijzen op de verwachte naam, verplaatsen;
    SHA-256 in de browser (Web Crypto), gegroepeerd per 8 tekens, met een kopieerknop;
  - een preview in een sandboxed iframe: PDF (pdf.js), docx (docx-preview), xlsx (SheetJS),
    afbeeldingen, tekst/code en de inhoud van een zip (JSZip);
  - blokkerende controles en waarschuwingen (leeg sjabloon, tijdelijk bestand, leeg bestand,
    dubbel bestand, afwijkende naam, afwijkend aantal), met één extra bevestiging;
  - een gestreamde upload met voortgangsbalk, controle van de hash op de server, atomaire
    opslag, een idempotency key en maximaal één actieve inlevering per student;
  - het ontvangstbewijs (scherm, afdrukken, PDF via een eenmalige token) en het resetten
    voor gedeelde computers (geen cache, geen browseropslag, automatische reset).
- Docentkant (fase 5):
  - een live overzicht (elke 10 seconden) met tellers, filters, sortering en signalen (leeg
    sjabloon, lockbestand, leeg bestand, naam wijkt af, identiek aan een andere student, niet op
    de lijst, verlenging, zip, aantal wijkt af);
  - studentdetail met alle pogingen; inleveringen ongedaan maken en ongeldig verklaren (met
    reden, de bestanden blijven bewaard);
  - downloads per bestand, per student (zip) en voor het hele examen (gestreamde zip met
    ZIP64, mapnamen veilig voor Windows, `manifest.csv` en een integriteitscontrole);
  - CSV-export van het overzicht, het proces-verbaal (PDF) met de verplichte onderdelen en
    handtekeningvelden, de controle van ontvangstbewijzen, en het auditlog per examen en het
    systeemlog bekijken en exporteren (CSV en PDF), met de status van de hash-keten.
- Levenscyclus (fase 6):
  - een scheduler in de app (bewaartermijn, opruimen, back-up) met een lock in de database,
    zodat een taak bij meerdere replicas maar één keer draait;
  - de bewaartermijn in kalenderdagen in `APP_TIMEZONE`, met een waarschuwing vanaf 7 dagen
    ervoor; verlengingen schuiven de termijn op;
  - bij het afronden eerst een definitief proces-verbaal (met integriteitscontrole), daarna
    het verwijderen van alle bestanden en de klassenlijst; een lijst met proces-verbalen per
    examen;
  - een bewaren-vlag met reden (eigenaar en admin), met 7 dagen uitstel na het weghalen;
    definitief verwijderen van een afgerond examen door de admin;
  - opruimtaken voor verweesde objecten, verlopen tokens, sessies en uitnodigingen;
  - `inleverboks retention run [--dry-run]`, `cleanup run`, `backup --to`, `restore --yes` en
    `integrity check [--exam]`.
- Afronding (fase 7):
  - een huisstijlscherm voor de admin: naam, lettertype (Poppins of systeem), kleuren met live
    voorbeeld en contrastcontrole (WCAG AA), logo (PNG/WebP, opnieuw ingelezen) met afgeleid
    favicon, en terug naar standaard; de appnaam en kleuren ook in de PDF's;
  - Poppins zelf gehost (SIL OFL);
  - `inleverboks demo-seed` (alleen met `DEMO_MODE=true`);
  - Kustomize-manifesten (`deploy/k8s`, base en test-overlay) voor Flux;
  - `release.yml`: multi-arch image naar `ghcr.io/hoite/inleverboks` met SBOM en provenance,
    met eerst een Trivy-scan (geen push bij CRITICAL, SARIF naar code scanning);
  - `dependabot.yml` voor uv, GitHub Actions, Docker en Compose;
  - een automatische toegankelijkheidscontrole van alle paginatypen.

### Gewijzigd

- De focusring is tweekleurig en invoervelden hebben een donkerdere rand (contrast ≥ 3:1).
- Het image bevat geen pip meer en neemt de Debian-beveiligingsupdates mee bij het bouwen.
- De knoppen in de dialogen van de studentpagina hebben hun tekst al in de HTML.
