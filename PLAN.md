# Inleverboks: bouwplan (ter goedkeuring)

Status: **concept, wacht op akkoord**. Dit plan volgt de specificatie
(`inleverboks-claude-code-prompt.md`) plus de antwoorden van 13-11-2026. Waar dit
plan afwijkt van de spec staat dat in §1.

---

## 1. Besluiten en afwijkingen van de spec

| # | Onderwerp | Besluit |
|---|---|---|
| B1 | Toegangscode | Wordt **leesbaar** opgeslagen (geen hash) en mag getoond worden. Nog steeds 6 tekens zonder verwarrende tekens, opnieuw te genereren, rate limiting op pogingen. |
| B2 | Inleverlink | `{APP_BASE_URL}/{slug}`, dus **zonder `/i/`**, bijvoorbeeld `inleverboks.hoite.nl/examen-p2k1-14112026`. De docent kiest een eigen slug, of laat er een genereren in de vorm `xxx-xxx-xxx` (alfabet zonder 0/o/1/i/l). Omdat de slug op het hoogste niveau staat, is er een lijst met **gereserveerde woorden** (`login`, `admin`, `static`, `healthz`, `readyz`, `api`, `docent`, `ontvangstbewijs`, …) die als slug geweigerd worden. |
| B3 | Rollen | Iedere gebruiker is docent. **Admin** is een extra rol. De **local admin** (aangemaakt via de bootstrap) heeft een vlag `is_local_admin`: die rol en dat account kunnen **nooit** worden afgepakt, gedegradeerd of gedeactiveerd, ook niet door een andere admin. Een admin kan andere docenten admin maken en die rol weer afnemen. |
| B4 | Admin en examens | Een admin heeft op zijn **eigen** examens gewoon alle rechten van een eigenaar. Op examens van anderen ziet een admin het overzicht, de metadata, de signalen en het auditlog, en mag hij **bestanden downloaden** (per bestand, per student en als zip). Hij mag daar ook het proces-verbaal en de CSV exporteren. Dit wijkt af van de matrix in §2 van de spec. Het wijzigen van de instellingen, het annuleren en ongeldig verklaren en het geven van verlengingen blijven voorbehouden aan de eigenaar. Elke download door een admin komt in het auditlog van het examen. |
| B13 | Slug | Uniek over **alle** examens, ook afgeronde en definitief verwijderde. Een slug wordt nooit hergebruikt, zodat een oude link of QR-code nooit bij een ander examen uitkomt. Na een definitieve verwijdering blijft daarom een rij in `retired_slug` bestaan. |
| B14 | Huisstijl | Op basis van de brandguide van Alfa-college (oktober 2024), zie §2a. |
| B15 | Whitelist en local admin | De local admin is vrijgesteld van de whitelist, zodat de installatie zichzelf niet kan buitensluiten. Een domein op de whitelist geldt alleen voor exact dat domein, niet voor subdomeinen. (Fase 2) |
| B16 | TOTP kwijt | Herstelcodes (10 stuks, eenmalig). Daarnaast kan de admin bij het maken van een resetlink ook TOTP resetten. De CLI `create-admin --reset` is de noodroute voor admins. (Fase 2) |
| B17 | Sessie-eisen | Wordt iemand tijdens een sessie admin, of wordt TOTP verplicht voor iedereen, dan dwingt de app de nieuwe eis af bij de volgende request. (Fase 2) |
| B19 | Presentatiemodus | De teller in de presentatiemodus ververst elke 10 seconden en telt **wel** als activiteit. Een beamer die het hele examen aanstaat, logt de docent dus niet na 2 uur uit. Het live-overzicht (fase 5) telt niet als activiteit (B12). (Fase 3) |
| B20 | Sjabloonbestanden | Content-addressed opgeslagen (`templates/{sha256}`). Dupliceren en examensjablonen delen hetzelfde object. Het object wordt verwijderd zodra niemand er meer naar verwijst. (Fase 3) |
| B21 | Dupliceren | Kopieert de instellingen, vakjes, sjabloonbestanden en de klassenlijst-momentopname. Nieuw zijn de tijden (start nu, afgerond op het kwartier, met dezelfde duur), de slug, de code en de status (concept). (Fase 3) |
| B22 | Slug wijzigen | Wijzigt de docent de slug van een gepubliceerd examen, dan blijft de oude slug gereserveerd (B13). De oude link geeft daarna 404. (Fase 3) |
| B23 | Terug naar concept | Een gepland examen zonder inleveringen kan terug naar concept. (Fase 3) |
| B24 | Extra bestanden | In het vak "extra bestand" mag een student meerdere bestanden zetten, binnen het maximum aantal bestanden per inlevering. Een vakje bevat precies één bestand. (Fase 4) |
| B25 | Ontvangstbewijs-PDF | De verificatiecode staat alleen gehasht in de database. De PDF-knop stuurt de eenmalige token (15 minuten) samen met de code via POST mee, dus de code komt niet in URL's of access logs. De server neemt de code alleen in de PDF op als hij overeenkomt met de hash. (Fase 4) |
| B26 | Preview | De previews draaien in een `sandbox="allow-scripts"`-iframe (opaque origin) met een eigen CSP. pdf.js draait daar zonder aparte worker: module-workers kunnen in een opaque origin niet starten. De gedeelde bibliotheken krijgen `Access-Control-Allow-Origin: *` (het zijn openbare bestanden). De hoofdpagina houdt een strikte CSP zonder inline code. (Fase 4) |
| B27 | Studentsessie | De voortgang staat in een ondertekende cookie (`ib_student`, 45 minuten, `Path=/{slug}`, `SameSite=Strict`, HttpOnly) en wordt na het inleveren gewist. Na 15 minuten zonder activiteit volgt een waarschuwing met 60 seconden aftellen, en daarna een reset. Het ontvangstbewijs wordt na 5 minuten leeggemaakt. (Fase 4) |
| B28 | Geweigerde pogingen | De eerste geweigerde poging per examen, IP-adres, minuut en soort komt direct in het auditlog. De aantallen per minuut worden in een samenvatting bijgeschreven door de scheduler (fase 6). (Fase 4) |
| B29 | Examenpagina | `/docent/examens/{id}` toont de inleveringen (overzicht). De instellingen staan onder `/instellingen` en het auditlog onder `/auditlog` (tabbladen). (Fase 5) |
| B30 | Zip per student | Bevat alle pogingen van die student: de actieve in de studentmap, en eerdere pogingen in `_geannuleerd/…` of `_ongeldig/…`. In de zip van het hele examen staan die alleen als de docent dat aanvinkt. De datum in de bestandsnaam is de startdatum van het examen. (Fase 5) |
| B31 | CSV-veiligheid | Cellen die beginnen met `= + - @` krijgen een `'` ervoor, zodat Excel ze niet als formule uitvoert (namen komen van studenten). (Fase 5) |
| B32 | Proces-verbaal | Elke export maakt een nieuwe PDF met een eigen document-ID. Die wordt opgeslagen (`pv/{exam_id}/{document_id}.pdf`) en in het auditlog vastgelegd, samen met de hash van het laatste auditlog-item. De integriteitscontrole draait bij elke export opnieuw. (Fase 5) |
| B33 | Ontvangstbewijs controleren | Alleen docenten met toegang tot het examen (eigenaar, tweede beoordelaar, admin) zien het resultaat. Elke controle komt in het auditlog, ook een mislukte. (Fase 5) |
| B34 | Scheduler | APScheduler in elke pod, met een lease-lock per taak in de tabel `job_lock` (30 minuten; een gecrashte pod blokkeert dus niet). Bewaartermijn elk kwartier, opruimen elk uur, back-up volgens `BACKUP_SCHEDULE`. In de tests staat de scheduler uit. (Fase 6) |
| B35 | Bewaartermijn | Verwijderen om 00:00 lokale tijd op einddatum + N + 1 dagen (de einddatum telt niet mee, en de laatste verlenging telt als einde). Waarschuwing vanaf 7 dagen ervoor. `retention_delete_after` op het examen werkt als ondergrens (voor het uitstel na de bewaren-vlag). (Fase 6) |
| B36 | Afronden | Volgorde: definitief proces-verbaal → bestanden verwijderen (alle pogingen) → klassenlijst van het examen verwijderen → `finalized_at`. Metadata, proces-verbalen en het auditlog blijven bewaard. Mislukt het proces-verbaal, dan wordt er niets verwijderd en probeert de taak het later opnieuw. (Fase 6) |
| B37 | Bewaren-vlag weghalen | Is de termijn al verstreken of verloopt die binnen 7 dagen, dan wordt de verwijdering 7 dagen na het weghalen. Zo is er altijd een week om de bestanden nog veilig te stellen. (Fase 6) |
| B38 | Definitief verwijderen | Alleen de admin, alleen voor afgeronde examens, met een reden. Het auditlog van het examen blijft staan (append-only), en de gebeurtenis komt ook in het systeemlog. De slug gaat naar de lijst met gebruikte slugs. (Fase 6) |
| B39 | Back-up | Een `tar.gz` met de SQLite-database (backup-API) en alle objecten behalve `tmp/`. PostgreSQL valt erbuiten (gebruik `pg_dump`). Herstellen alleen met `--yes` en met de app gestopt; de oude database blijft als `.before-restore` bewaard. (Fase 6) |
| B41 | Huisstijl laden | De huisstijl staat in de instelling `branding` en komt als stylesheet `/huisstijl/stijl.css?v=<hash>` in elke pagina (geen inline stijlen, CSP blijft strikt). Een cache van 30 seconden per pod; bij een databasefout de standaard. `/huisstijl` staat op de lijst met gereserveerde slugs. (Fase 7) |
| B42 | Logo | Alleen PNG en WebP, maximaal 1 MB en 4 megapixel. Pillow leest het in, verkleint tot maximaal 512 × 512 en slaat het opnieuw op als PNG (`branding/{sha256}.png`); het favicon (64 × 64) wordt daarvan afgeleid. SVG niet, omdat veilig opschonen te foutgevoelig is. (Fase 7) |
| B43 | Contrastcontrole | Acht combinaties die de stylesheet echt gebruikt (tekst, links, witte tekst op knoppen en kopbalk, tint, accentlijnen). Tekst moet 4,5:1 halen, accentlijnen 3:1. Te weinig contrast geeft een waarschuwing (en komt in het auditlog), maar blokkeert het opslaan niet. "Terug naar standaard" laat het logo staan. (Fase 7) |
| B44 | Poppins | Alleen de latin-subset (400, 600, 700), dat dekt het Nederlands. Zelf gehost via `vendor.lock.json`. De admin kan ook het systeemlettertype kiezen. (Fase 7) |
| B45 | Focus en randen | De focusring is tweekleurig (geel met een donkere rand), zodat hij op lichte én donkere achtergronden 3:1 haalt (WCAG 1.4.11). De rand van invoervelden is donkerder gemaakt (3,7:1). (Fase 7) |
| B46 | Demo | `demo-seed` werkt alleen met `DEMO_MODE=true` en doet niets als de demodocent al bestaat. Alles loopt via de gewone services (ook de inleveringen), met een bevroren klok voor het gesloten examen van gisteren. (Fase 7) |
| B47 | Release | Eerst een amd64-image bouwen en met Trivy scannen; alleen zonder CRITICAL volgt de multi-arch build en push, met SBOM en provenance (`mode=max`). De SARIF van alle ernstniveaus gaat naar code scanning. pip wordt uit het runtime-image verwijderd en de Debian-updates worden bij het bouwen meegenomen. De basisimages staan direct in `FROM` (tag en digest), zodat Dependabot ze bijwerkt. (Fase 7) |
| B48 | Kubernetes | Het test-overlay maakt de namespace met Pod Security *restricted*. `automountServiceAccountToken: false`, een startupProbe, en `/tmp` als `emptyDir` van 2Gi. (Fase 7) |
| B49 | Datum en tijd | Een `type="date"`-veld plus een tekstveld voor de tijd (24 uur, `uu:mm`; `9.30` en `0930` worden ook geaccepteerd). `datetime-local` volgt de taal van de browser en toont dan AM/PM. Het oude gecombineerde veld blijft geaccepteerd. (v0.1.1) |
| B50 | Zonder klassenlijst | `allow_unlisted` staat dan altijd aan (in de service afgedwongen bij aanmaken en bewerken); het formulier toont het vinkje uitgegrijsd. (v0.1.1) |
| B51 | Examensjablonen | Te maken, bekijken en bewerken als eigen object, zonder tijden, linknaam en klassenlijst. Eigen vaste kleur (paars `#6a3d9a`, 7,6:1 op wit) en markering, en een banner op elke sjabloonpagina. Sjabloonbestanden volgen hun vakje via het id. Geen databasewijziging nodig. (v0.1.1) |
| B52 | Versie | `pyproject.toml` is de bron; de footer toont `v{versie}`. Een release-tag `v0.1.1` maakt de image-tags `0.1.1` en `0.1`. (v0.1.1) |
| B53 | Gedeelde sjablonen | Alleen admins maken, wijzigen, delen en verwijderen gedeelde sjablonen. Elke docent kan ze gebruiken en er een eigen kopie van maken. Persoonlijke sjablonen blijven van de eigenaar. |
| B54 | Kwalificaties | Eigen tabellen (`qualification`, `core_task`, `work_process`) per crebo van de uitstroom en dossierversie. Zoeken gaat op de uitstroom, niet op het dossier. De codes zijn volledig (`B1-K1-W1`, `P1-K1-W1`), omdat basis- en profieldeel allebei een K1 hebben. Rijen blijven bestaan zolang sjablonen ernaar verwijzen. De import neemt records aan; `services/sbb.py` maakt die uit het SBB-bestand. |
| B55 | Sjabloon per kerntaak | Elke gekozen kerntaak wordt een sjabloon, met een vakje per werkproces (`label = code + naam`, ingekort tot 100 tekens). Geen verwachte bestandsnaam: die is voor de ingeleverde bestanden niet nodig. Sjabloonbestanden uploadt de docent zelf (beschermd materiaal). |
| B56 | Deelexamen | Eén configuratie per examen. Bij een nieuw examen kies je de werkprocessen; naam `{crebo} {naam} – {kerntaak} {titel}` of `– {codes}`, linknaam `{crebo}-{kerntaak}[-wN…]-{ddmmjjjj}`. Het examen bewaart een momentopname van de kwalificatie (door de server bepaald, niet uit verborgen velden). |
| B57 | Herkansing | Per student één klik in het overzicht, of een keuzepagina voor meerdere studenten (studenten met iets openstaands zijn al aangevinkt). Het nieuwe examen bevat de vakjes die voor ten minste één gekozen student openstaan, alleen die studenten op de lijst, en `allow_unlisted` uit. Sjabloonbestanden gaan per vakje mee. Geen checkboxes in het overzicht zelf: dat ververst elke 10 seconden. |
| B58 | Vervangen dossier | Een sjabloon is verouderd als er een nieuwere versie van dezelfde crebo is (op `revision`), als een herziening de uitstroom een nieuwe crebo gaf (`previous_crebo`), of als de geldigheid voorbij is. De detailpagina toont de gewijzigde, nieuwe en vervallen werkprocessen van de kerntaak. |
| B59 | SBB-import | Bron: XML *Herziening dossiers* (`dossiers.xml`) uit het register van SBB, als upload door de admin (XML of zip) of met `inleverboks sbb-import`. Geen automatisch ophalen: de downloadlink verandert per publicatie en het bestand is groot. Per profiel (uitstroom) en crebo één record; basis- (`B…`) en profielkerntaken (`P…`) samen. Profielen met dezelfde crebo (varianten) worden samengevoegd onder de naam van het dossier. Alleen `status="Vastgesteld"`. `valid_until` = einde opleiding (niet einde instroom). Versielabel `2015`, `2015, gewijzigd 2016`; de volgorde (`revision`) komt uit de keten `nrvoorganger`, en bij gelijke diepte uit het SBB-nummer. Het bestand wordt gestreamd; een DOCTYPE wordt geweigerd. |
| B40 | Opruimen | Objecten onder `tmp/`, sjablonen zonder verwijzing en ingeleverde objecten zonder databaserij worden verwijderd als ze ouder zijn dan 1 uur (zodat een lopende upload niet geraakt wordt). Proces-verbalen worden nooit opgeruimd. (Fase 6) |
| B18 | UI-paden | Nederlandse paden (`/docent`, `/admin/gebruikers`, `/uitnodiging/…`). Ze staan op de lijst met gereserveerde slugs. (Fase 2) |
| B5 | Concept-examen | Een concept wordt actief via de knop **Publiceren**. De link van een concept werkt wel, maar toont alleen: "Deze inleverlink is nog niet opengezet door de docent." |
| B6 | Extra bestand | Per examen in te stellen, **standaard aan**, met de extensies van het examen. |
| B7 | Standaardextensies | Een lijst die de admin beheert, standaard: `docx xlsx pptx pdf txt md csv zip py sh js ts html css java c h cpp cs sql json yaml yml xml png jpg jpeg gif webp`. |
| B8 | .zip | Toegestaan. De **browser** toont vóór het uploaden de lijst met bestanden in de zip (via JSZip, dat al meegeleverd wordt voor docx-preview), met de melding: "De inhoud van een zip wordt niet gecontroleerd." De server pakt nooit iets uit (§5.4 blijft gelden). Bij de docent krijgt een zip het signaal "zip". |
| B9 | Ongeldig zonder herkansing | De student krijgt dezelfde melding als bij een actieve inlevering ("Je hebt al ingeleverd. Meld je bij de docent."). Geen aparte melding. |
| B10 | Python | **3.15** (nieuwste stabiele versie, 3.15.2). Alle native afhankelijkheden hebben al wheels voor cp315. Image: `python:3.15.2-slim-trixie`, vastgepind op digest. |
| B11 | Git | Sinds 14-11-2026 maakt Claude de commits per fase (Conventional Commits); pushen doet de eigenaar, na akkoord. |
| B12 | HTMX-polling | Polling verlengt de inactiviteitstimer van de sessie **niet** (header `HX-Trigger: poll` → geen `touch`). |

## 2. Open vragen

Geen. V1 (admin-downloads), V2 (uniekheid van de slug) en V3 (de accentkleur) zijn verwerkt in B4, B13 en §2a.

## 2a. Huisstijl (standaardwaarden uit de brandguide)

De brandguide geeft als basiskleuren **blauw `#005381`** en **rood `#e73443`**, met tinten van 75, 50, 25 en 10%. Het logo staat nog in de oude kleuren (`#003782` en `#de0000`). Volgens de brandguide is tekst op wit altijd blauw, en tekst op een kleurvlak altijd wit. Het lettertype is **Poppins**: Bold voor koppen, Semibold voor intro's en Regular voor de broodtekst.

| CSS-variabele | Waarde | Gebruik | Contrast op wit |
|---|---|---|---|
| `--color-primary` | `#005381` | headerbalk (witte tekst), knoppen, links, koppen | 8,2:1 ✓ |
| `--color-primary-strong` | `#003782` | hover/actief | 11,2:1 ✓ |
| `--color-primary-tint` | `#e6eef2` (10%) | achtergrond van panelen en tabelkoppen | blauw daarop: 7,0:1 ✓ |
| `--color-accent` | `#e73443` | accentlijnen, driehoekje in de header, grote tekst | **4,2:1**: alleen voor grote of vette tekst (≥ 18,7 px vet) en niet-tekst |
| `--color-accent-text` | `#c42a37` | als rood als gewone tekst nodig is (afgeleid, iets donkerder) | 5,6:1 ✓ |
| `--color-text` | `#313131` | broodtekst | 13,0:1 ✓ |
| `--color-bg` | `#ffffff` | | |

- **Kanttekening:** de spec beschreef `#599AD7` als primaire kleur met donkere tekst. Ik vervang dat door het blauw uit de brandguide met witte tekst, omdat dat aansluit bij §2.3 van de brandguide en beter scoort op contrast.
- **Lettertype:** Poppins (SIL Open Font License) wordt **zelf gehost** als woff2 in `static/fonts/`, dus geen Google Fonts of CDN. Als terugval geldt de system font stack. Het lettertype is in te stellen in het huisstijlscherm.
- **Grafisch element:** een kleine rode driehoek als accent in de header en de presentatiemodus, als losse SVG. Het is geen logo, dus het mag in de repo.
- **Logo:** niet in de repo, zoals de spec voorschrijft. De admin uploadt het logo (PNG/WebP, en SVG wordt opgeschoond). De repo bevat alleen een neutrale placeholder. De minimale breedte uit de brandguide (25 mm) geldt voor druk. In het proces-verbaal maak ik het logo 35 mm breed, met witruimte eromheen.
- **De sectorkleuren** (§2.2 van de brandguide) gebruik ik niet. Ze zijn alleen bedoeld voor uitingen voor één sector.
- **Het huisstijlhandboek zelf** komt niet in de repo.

---

## 3. Architectuur

```
Browser (docent)  ── Jinja2-pagina's + HTMX
Browser (student) ── Jinja2 + ES-modules (hash, preview, controles, upload)
        │
   Uvicorn (proxy-headers, TRUSTED_PROXIES)
        │
   FastAPI-app
   ├─ web/        routes, templates, forms, CSRF, security headers
   ├─ policy/     één autorisatielaag: can(actor, action, resource)
   ├─ services/   domeinlogica (exams, submissions, retention, audit, export…)
   ├─ repos/      SQLAlchemy-queries (geen businesslogica)
   └─ adapters/   StorageBackend · AuthProvider · Mailer · FileScanner · RateLimiter · Clock
        │
   SQLite (WAL) / PostgreSQL           Local FS / S3 (MinIO)
```

- **Autorisatie centraal:** `policy.py` bevat de matrix uit §2 als data (`Action` × `ExamRole`) plus één FastAPI-dependency `require(action)`. De tests itereren over de hele matrix.
- **`Clock`-adapter:** overal `clock.now()` in plaats van `datetime.now()`, zodat de tijdvenster-, DST- en bewaartermijntests deterministisch zijn.
- **Auditlog:** een `AuditService.append()` is de enige schrijfroute. De repository heeft geen update of delete. Op PostgreSQL komt er daarnaast een trigger die UPDATE en DELETE op `audit_event` weigert. Op SQLite doet een trigger hetzelfde (`RAISE(ABORT)`). De hash-keten is `sha256(prev_hash ‖ canonical_json(event))`, met een aparte keten per examen en voor het systeem. Het toevoegen gebeurt onder een lock (rij-lock in PG, `BEGIN IMMEDIATE` in SQLite).
- **Upload:** streamen met `python-multipart` (de streaming parser, niet `UploadFile` in het geheugen). Per bestand wordt tegelijk geschreven naar `{tmp}/{uuid}`, gehasht en de grootte geteld. Pas na de hashcontrole van alle bestanden worden ze verplaatst naar de definitieve key, en daarna volgt de DB-commit. Voor S3 gaan de bestanden eerst naar een `tmp/`-prefix en daarna via copy en delete naar de definitieve plek. Een opruimtaak ruimt wezen op ouder dan 1 uur.
- **Eén actieve inlevering:** een partial unique index op `(exam_id, student_number) WHERE status = 'active'`. Die werkt op zowel SQLite als PostgreSQL. Daarnaast een unieke `idempotency_key`.
- **Studentsessie:** geen account. Wel een kortlevende, ondertekende cookie (alleen voor CSRF en voor de stap "code is geldig, nummer is bevestigd"), 30 minuten geldig, gewist bij Klaar.
- **CSP-strategie voor previews:** de studentpagina laadt alleen `'self'`-scripts. docx-preview en SheetJS produceren inline styles, dus de preview draait in een `<iframe sandbox="allow-scripts">` op `/static/preview/frame.html`, met een eigen CSP-header (`style-src 'self' 'unsafe-inline'`, geen `connect-src`). Het bestand gaat via `postMessage` als ArrayBuffer naar het iframe. pdf.js draait met een worker (`worker-src 'self'`).
- **Scheduler:** APScheduler (in-process) plus een tabel `job_lock` met een lease (UPDATE … WHERE expires_at < now), zodat elke taak bij meerdere replicas maar één keer draait.

## 4. Mappenstructuur

```
.
├── src/inleverboks/
│   ├── main.py               # app factory
│   ├── config.py             # Pydantic Settings
│   ├── cli.py                # Typer: migrate, create-admin, retention run, backup, restore,
│   │                         #        audit verify, integrity check, demo-seed
│   ├── db/{base,session,models/*.py}
│   ├── repos/
│   ├── services/{auth,users,exams,classlists,submissions,receipts,exports,
│   │             pv,audit,retention,backup,branding,settings}.py
│   ├── policy.py
│   ├── adapters/{storage/{base,local,s3}.py, auth/{base,local}.py,
│   │             mail/null.py, scanner/noop.py, ratelimit/memory.py, clock.py}
│   ├── web/
│   │   ├── routes/{public,student,auth,dashboard,exams,submissions,admin,health}.py
│   │   ├── middleware/{security_headers,csrf,proxy,session}.py
│   │   ├── templates/{base,student/*,teacher/*,admin/*,pdf/*}.html.j2
│   │   └── static/{css,js/student/*.js,vendor/*,preview/frame.html,img/placeholder-logo.svg}
│   ├── i18n/nl/LC_MESSAGES/messages.po      # Babel/gettext
│   └── migrations/                          # Alembic
├── tests/{unit,integration,authz,e2e}/
├── deploy/k8s/{base,overlays/test}/
├── docs/{architectuur.md,commits/}
├── scripts/vendor.py                        # downloads vendored JS + checks SHA-256
├── vendor.lock.json                         # versie, URL, sha256, licentie per JS-bibliotheek
├── Dockerfile  compose.yaml  compose.scale.yaml  entrypoint.sh
├── .github/{workflows/ci.yml,workflows/release.yml,dependabot.yml}
├── pyproject.toml  uv.lock  .env.example
└── README.md  CLAUDE.md  SECURITY.md  CHANGELOG.md  THIRD_PARTY_NOTICES.md  LICENSE
```

## 5. Datamodel

Alle primaire sleutels zijn UUID's (v7, tijd-sorteerbaar). Tijden zijn `timestamptz`, opgeslagen in UTC.

| Tabel | Belangrijkste kolommen |
|---|---|
| `user` | email (uniek, lowercase), name, password_hash, totp_secret (versleuteld), totp_enabled, is_admin, **is_local_admin**, is_active, must_change_password, created_at, last_login_at |
| `recovery_code` | user_id, code_hash, used_at |
| `invite` / `password_reset` | email of user_id, token_hash, expires_at, used_at, created_by |
| `session` | id-hash, user_id, created_at, last_seen_at, ip, user_agent, revoked_at, totp_verified |
| `whitelist_entry` | kind (`domain`/`email`), value |
| `ip_preset` | name, cidrs (JSON) |
| `setting` | key, value (JSON): retention_days, student_number_regex, limits, default_extensions, require_totp_all, branding |
| `branding_asset` | kind (logo/favicon), storage_key, mime, sha256 |
| `class_list` / `class_list_entry` | owner_id, name · student_number, first_name, infix, last_name, class_name |
| `exam_template` (+ `exam_template_slot`, `template_file`) | owner_id, name, config (JSON: extensies, groottes, code aan/uit, IP) |
| `exam` | owner_id, second_reviewer_id, name, description, slug (uniek), **access_code** (leesbaar), access_code_enabled, ip_restriction_enabled, ip_cidrs (JSON), allow_unlisted, allow_extra_file, max_file_bytes, max_submission_bytes, extensions (JSON), starts_at, ends_at, published_at, status, hold_flag, hold_reason, retention_delete_after, finalized_at, requirements_locked_at |
| `expected_file` (vakje) | exam_id, position, label, expected_name_pattern, extensions (JSON, optioneel) |
| `template_file` | expected_file_id of template_slot_id, storage_key, sha256, size, original_name |
| `exam_roster_entry` | exam_id, student_number, first_name, infix, last_name, class_name |
| `extension_grant` | exam_id, student_number, ends_at, created_by |
| `submission` | exam_id, student_number, first_name, infix, last_name, on_roster, attempt, status (`active`/`cancelled`/`invalid`), may_resubmit, status_reason, status_changed_by/at, started_at, completed_at, declaration_accepted, idempotency_key (uniek), receipt_code (uniek, `IB-XXXX-XXXX`), verification_code_hash, client_ip, confirmed_warnings (JSON) |
| `submission_file` | submission_id, expected_file_id (null = extra), original_name, storage_key, size, sha256, warnings (JSON), last_integrity_check_at/ok |
| `receipt_token` | submission_id, token_hash, expires_at, used_at |
| `audit_event` | chain (`system`/exam_id), seq, ts, actor_type, actor_id/label, ip, event, details (JSON), prev_hash, hash. Uniek op (chain, seq). |
| `proces_verbaal` | exam_id, document_id, storage_key, is_final, generated_by, generated_at, audit_head_hash |
| `job_lock` | name, holder, expires_at |
| `retired_slug` | slug (uniek), retired_at (blijft na een definitieve verwijdering) |
| `denied_attempt_bucket` | exam_id, ip, minute, kind, count (voor het samenvoegen per minuut) |

De status `concept/gepland/open/gesloten/afgerond` wordt **afgeleid** van `published_at`, de tijden, de verlengingen en `finalized_at`. Alleen `afgerond` wordt opgeslagen. Zo kan de status nooit uit de pas lopen met de klok.

De Mermaid-ERD komt in `docs/architectuur.md` (fase 2).

## 6. Bibliotheken (vastgepind via `uv.lock`; versies van 13-11-2026)

**Runtime:** fastapi 0.162.7 · starlette 1.10.0 · uvicorn[standard] 0.54.0 · jinja2 3.1.6 · sqlalchemy 2.1.8 · alembic 1.20.1 · pydantic 2.14.3 · pydantic-settings 2.16.3 · python-multipart 0.0.33 · argon2-cffi 25.1.0 · pyotp 2.10.0 · segno 1.6.6 · weasyprint 70.0 · **zipstream-ng 1.9.3** (streaming, ZIP64, actief onderhouden) · typer 0.29.1 · itsdangerous 2.2.0 · cryptography 50.0.2 (versleuteling van het TOTP-geheim) · psycopg[binary] 3.3.9 · boto3 1.44.2 · apscheduler 3.11.3 · structlog 26.1.0 (JSON-logs) · babel 2.18.0 (i18n) · charset-normalizer 3.5.2 (ondersteunt de detectie van Windows-1252).

**Dev:** ruff 0.17.5 · mypy 2.4.1 · pytest 9.2.0 · pytest-cov 7.1.0 · httpx 0.28.1 · playwright 1.65.1 + pytest-playwright 0.10.0 · moto[s3] 5.2.5 (snelle S3-tests). In CI draait daarnaast MinIO.

**Vendored JS** (via `scripts/vendor.py` met de sha256 in `vendor.lock.json`, nooit via een CDN tijdens gebruik): htmx 2.0.11 (0BSD) · pdfjs-dist 6.5.438 (Apache-2.0) · docx-preview 0.4.1 (Apache-2.0) · jszip 3.10.2 (MIT, gekozen uit MIT/GPL) · SheetJS CE, de laatste versie van cdn.sheetjs.com (Apache-2.0) · het lettertype Poppins, Regular, Semibold en Bold als woff2 (OFL-1.1).

**Wachtwoordlijst:** een top-100k-lijst (SecLists, MIT) gecomprimeerd in het pakket, gecontroleerd met een set in het geheugen.

Alle licenties zijn verenigbaar met EUPL-1.2.

## 7. Fasering

Volgens §16 van de spec. Na elke fase zijn ruff, mypy en pytest groen, en krijg je een samenvatting plus de klaargezette commitberichten. **Na elke fase wacht ik op je akkoord.**

1. **Fundament.** Repostructuur, `pyproject.toml`/uv, ruff/mypy/pytest, config, `/healthz` en `/readyz`, security-headers-middleware, Dockerfile (non-root, read-only rootfs, multi-arch), compose, `ci.yml`-skelet, CLAUDE.md, LICENSE, SECURITY.md, `.env.example`.
2. **Kern.** Datamodel en migraties, CLI, bootstrap van de local admin, auth (uitnodigingen, login, TOTP met herstelcodes, sessies, whitelist, rate limiting), auditlog met hash-keten en `audit verify`, policy-laag met matrixtests, admin-schermen.
3. **Examens.** CRUD, publiceren, dupliceren, vakjes en sjablonen, examensjablonen, slug (eigen of gegenereerd, met gereserveerde woorden), toegangscode, IP-whitelist en presets, QR (PNG/SVG), presentatiemodus, klassenlijsten (import en bibliotheek), verlengingen.
4. **Studentflow.** Vendoring, hashing, preview in een sandbox-iframe, zip-inhoud, controles, streaming upload met verificatie, idempotency, ontvangstbewijs (scherm en PDF), reset en no-store.
5. **Docentkant.** Live overzicht, detail, signalen, annuleren en ongeldig verklaren, downloads (bestand, student, zip met manifest en integriteitscontrole), CSV, proces-verbaal, ontvangstbewijs controleren, auditlog bekijken en exporteren.
6. **Levenscyclus.** Scheduler met lock, bewaartermijn, bewaren-vlag, afgeronde examens met een definitief proces-verbaal, opruimtaken, back-up en herstel, `integrity check`.
7. **Afronding.** Huisstijl-admin met contrastcontrole, i18n compleet, toegankelijkheidscontrole, Kustomize, `release.yml` met Trivy, SBOM en provenance, `dependabot.yml`, E2E-tests, alle documentatie, `demo-seed`.

## 8. Lokale omgeving: wat ik nodig heb

- **Docker** 29.9.3 draait. Compose v5 en buildx zijn aanwezig.
- **uv ontbreekt** op deze Mac, net als Node (Node is niet nodig). Installeer uv zelf: `brew install uv`. uv haalt daarna zelf Python 3.15 op. Zonder uv kan ik alles in een Docker-container laten draaien, maar dat is trager.
- **PostgreSQL en MinIO** voor de tests draaien via `compose.scale.yaml`.
