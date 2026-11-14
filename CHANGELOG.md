# Changelog

Alle noemenswaardige wijzigingen staan in dit bestand. Het formaat volgt
[Keep a Changelog](https://keepachangelog.com/nl/1.1.0/) en de versies volgen
[Semantic Versioning](https://semver.org/lang/nl/).

## [Unreleased]

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
