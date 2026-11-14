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
