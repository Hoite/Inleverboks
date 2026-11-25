# CLAUDE.md

Werkafspraken voor Claude Code (en andere ontwikkelaars) in deze repo. De volledige
specificatie staat in `inleverboks-claude-code-prompt.md`, en de gemaakte keuzes staan in `PLAN.md`.
Bij een tegenstrijdigheid geldt eerst `PLAN.md` en daarna de specificatie. Twijfel je bij
beslissingen die lastig terug te draaien zijn (datamodel, auth, opslag)? Vraag het dan.

## Talen

- **Code, identifiers, commentaar en commitberichten:** Engels.
- **UI:** Nederlands, en **altijd via gettext**. Elke tekst staat in
  `src/inleverboks/i18n/nl/LC_MESSAGES/messages.po`, met een Engelse msgid. In templates
  schrijf je `{{ _("...") }}`. Zet nooit losse Nederlandse tekst in een template.
- **README en `docs/`:** Nederlands.

## Commando's

```bash
uv sync                                   # dependencies (incl. dev)
uv run pytest                             # unit- en integratietests (SQLite)
TEST_DATABASE_URL=postgresql://u:p@127.0.0.1:5432/db uv run pytest   # tegen PostgreSQL
uv run pytest -m e2e                      # Playwright (eenmalig: uv run playwright install chromium)
uv run pytest --cov                       # met coverage
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict, src + tests
uv run inleverboks migrate                # migraties (gebruikt .env)
uv run inleverboks db-revision "add x"    # nieuwe migratie autogenereren (controleer het resultaat!)
uv run python scripts/i18n.py             # nieuwe UI-teksten naar messages.po; daarna vertalen
uv run python scripts/i18n.py --check     # ontbrekende vertalingen (zit ook in de tests)
uv run python scripts/i18n_fill.py nl.py  # lege msgstr vullen uit een dict NL = {...} (hulpmiddel)
uv run python -I scripts/vendor.py        # meegeleverde JS ophalen/controleren (vendor.lock.json)
TEST_S3_ENDPOINT=http://127.0.0.1:9000 uv run pytest tests/integration/test_storage_contract.py
uv run uvicorn inleverboks.asgi:app --reload --port 8080   # lokaal draaien (.env nodig)
docker build -t inleverboks:dev .
DEMO_MODE=true uv run inleverboks demo-seed        # voorbeelddata (lege demo-installatie)
kubectl kustomize deploy/k8s/overlays/test         # manifesten renderen (secret.env nodig)
docker compose up --build
```

WeasyPrint (PDF) heeft Pango nodig. Lokaal op macOS: `brew install pango`, en zorg dat Python
de Homebrew-bibliotheken vindt: `export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` (bijvoorbeeld
in `~/.zprofile`). Zonder Pango slaan de PDF-render-tests zichzelf over; in CI en Docker draaien
ze wel.

Docker staat op deze machine in `~/.docker/bin`. Zet in een niet-login-shell eerst
`export PATH="$PATH:$HOME/.docker/bin"`.

## Architectuur

```
web/       routes, templates, middleware (security headers, body limit), templating
policy.py  de enige autorisatielaag (matrix als data); web/deps.py levert Teacher/Admin
services/  domeinlogica, zonder HTTP-kennis
repos/     databasequeries, zonder businesslogica
adapters/  StorageBackend (local, s3), AuthProvider, Mailer, FileScanner, RateLimiter, Clock
db/        Base (naming convention), engine/session, migrate-helpers, models/
migrations/ Alembic (script_location in de code, er is geen alembic.ini)
```

- **App factory:** `inleverboks.main.create_app(settings)`. Tests geven hun eigen `Settings` mee.
  `inleverboks.asgi:app` is voor uvicorn.
- **Container:** `CMD python -m inleverboks.entrypoint`. Die voert optioneel de migraties uit en
  exect daarna uvicorn. De CLI draait in hetzelfde image: `docker run <img> inleverboks migrate`.
- **Proxy:** `TRUSTED_PROXIES` wordt op twee plekken gebruikt: in uvicorn (`--forwarded-allow-ips`)
  en in de app (`ProxyHeadersMiddleware`). Dat is idempotent, en zo test de testsuite hetzelfde
  pad als productie. Lees het client-IP altijd uit `request.client.host`.
- **Opslag:** keys zoals `{exam_id}/{submission_id}/{uuid}`. Schrijf eerst naar `tmp/…` en zet het
  bestand pas na verificatie met `move()` op zijn plek. Ingeleverde bestanden worden nooit
  statisch of inline geserveerd.
- **Tijd:** sla alles op in UTC (`DateTime(timezone=True)`) en toon in `APP_TIMEZONE`. Gebruik
  (vanaf fase 2) de `Clock`-adapter, nooit `datetime.now()` direct.
- **URL's:** de UI-paden zijn Nederlands (`/examens`, `/account/wachtwoord`, `/admin/gebruikers`,
  `/uitnodiging/{token}`). Inleverlinks komen in fase 3 direct onder `/` (`/{slug}`). Elk nieuw
  pad op het hoogste niveau moet dus op de lijst met gereserveerde slugs.
- **Routes** zijn sync `def` (FastAPI draait ze in een threadpool), omdat SQLAlchemy sync is.
  Services committen zelf. Bij een `DomainError` doet de route `db.rollback()`.
- **Fouten:** services gooien `DomainError("code")`, en `web/messages.py` vertaalt de code.
- **Migraties:** autogenerate en dan **handmatig nalopen**. Vervang `UTCDateTime` door
  `sa.DateTime(timezone=True)`, zodat migraties los van de app-code blijven werken. Bijzondere
  DDL (triggers, partial indexes) gaat met `op.execute`.
- **Tests:** `tests/conftest.py` geeft elke test een vers gemigreerde database (SQLite-kopie, of
  TRUNCATE op PostgreSQL), een `FrozenClock` en helpers (`create_user`, `login`, `post` met
  CSRF, `enable_totp`). Gebruik `post(client, url, data)` voor formulieren.
- **Statische bestanden:** gebruik in templates altijd `{{ asset('css/app.css') }}`, niet
  `url_for('static', ...)`. `asset()` voegt `?v=<hash>` toe, zodat browsers na een update
  nooit een verouderde versie gebruiken (lange cache voor URL's met hash).
- **Routes met de catch-all `/{slug}`** (`web/routes/student.py`) worden als laatste
  geregistreerd. In tests registreer je extra routes met `add_first_route()`.
- **Sync routes en async opslag:** in een sync route roep je async code aan met
  `anyio.from_thread.run(...)`, bijvoorbeeld voor uploads en het verwijderen uit opslag.
- **Sjabloonbestanden** zijn content-addressed (`templates/{sha256}`). Dupliceren of opslaan als
  examensjabloon kopieert alleen de databaserij. Het object wordt pas verwijderd als er geen
  `TemplateFile` meer naar verwijst (`template_files.unreferenced`).
- **Formulieren zonder JS:** knoppen met `name="action"` (bijvoorbeeld `add_slot`) renderen het
  formulier opnieuw zonder op te slaan. Zet bovenaan een verborgen standaardknop, zodat Enter
  altijd "opslaan" (of "controleren") doet.
- **Studentflow:** de stappen (code → studentnummer → bevestigen/naam) zijn gewone formulieren;
  de voortgang staat in de ondertekende cookie `ib_student`. Het kiezen, hashen, controleren,
  bekijken en uploaden gebeurt in `static/js/student/`. **`checks.js` is een spiegel van
  `services/checks.py`.** Wijzig ze altijd samen; de E2E-test vergelijkt de uitkomsten.
- **Upload:** `web/upload.py` streamt multipart naar tijdelijke bestanden (hasht onderweg en
  dwingt de groottes af tijdens het lezen). Daarna volgen `SubmissionService.prepare` (alles
  opnieuw controleren), opslaan (eerst `tmp/…`, dan de definitieve key) en `commit` (één
  transactie; bij een conflict worden de bestanden weer verwijderd). Er is géén route die een
  ingeleverd bestand aan een student teruggeeft; een test controleert dat.
- **Docentkant:** `services/overview.py` bouwt de rijen, tellers en signalen; `services/exports.py`
  bevat de naamgeving voor Windows, de CSV-conventies en de streaming zip met
  integriteitscontrole (manifest als laatste entry); `services/pv.py` maakt het
  proces-verbaal. De overzichtstabel ververst via HTMX met een element-id `poll-…`.
- **Testhulp studentflow:** `tests/student_helpers.py` (examen maken, stappen doorlopen,
  uploaden) en de fixtures `owner` en `exam` in `tests/integration/conftest.py`.
- **Teksten in JavaScript** komen uit `client_texts()` (server-side vertaald) via de
  configuratie in `data-config`. Zet nooit losse teksten in JS.
- **Levenscyclus:** `services/lifecycle.py` (bewaartermijn, bewaren-vlag, afronden, definitief
  verwijderen), `services/maintenance.py` (opruimen, integriteitscontrole), `services/backup.py`
  en `services/scheduler.py` (`Jobs` met een lock per taak, `AppScheduler`). De CLI en de
  scheduler gebruiken dezelfde `Jobs`. In tests staat `scheduler_enabled` uit; roep de
  services direct aan met de `FrozenClock`. Async opslag vanuit sync code gaat via
  `services/asyncutil.call_async` (werkt zowel in een worker-thread als daarbuiten).
- **Huisstijl:** templates krijgen `brand` en `app_name` uit de context (`request.app.state.brand()`);
  zet nooit kleuren of de appnaam vast in een template. Nieuwe kleurcombinaties in `app.css`
  horen ook in `CONTRAST_PAIRS` (`services/branding.py`) en in de controle in
  `static/js/admin/branding.js`. PDF's krijgen `brand` via `PdfRenderer`.
- **Kwalificaties (SBB):** `services/qualifications.py` (import van records, zoeken, dossierstatus)
  en de modellen in `db/models/qualifications.py`. `services/sbb.py` leest het SBB-bestand
  (streaming, geen DOCTYPE) en maakt er records van; testdata met dezelfde structuur staat in
  `tests/sbb_sample.py`. Vakjes dragen een `work_process_code`; een
  examen een momentopname in `Exam.qualification`. Die bepaalt de server altijd zelf
  (`ExamService._resolve_sources`), nooit uit verborgen velden. Gedeelde sjablonen: alleen admins
  beheren ze (`can_manage_template`).
- **Docentpaden:** alles onder `/examens` (`/examens/{id:uuid}/…`, `/examens/sjablonen`,
  `/examens/klassenlijsten`). Gebruik `{exam_id:uuid}` in routes, zodat een examen-id nooit
  een vaste subpagina overschaduwt. Oude `/docent/…`-URL's sturen door via
  `web/routes/legacy.py`. Een examen heeft de tabbladen Inleveringen, Instellingen, Auditlog
  en Geavanceerd (`/examens/{id}/geavanceerd`: zeldzame acties en de bewaartermijn).
- **Examens uit een sjabloon:** `Exam.source_template_id`. De vakjes komen dan altijd van de
  server (`ExamService._lock_template_slots`: sjabloon plus gekozen werkprocessen bij nieuw,
  de bestaande vakjes bij bewerken); het formulier toont ze alleen-lezen. Herkansingen en
  examens zonder sjabloon houden bewerkbare vakjes.
- **Zoekbare keuzelijsten:** zet `data-combobox data-config="{{ combobox_config() }}"` op een
  `<select>` en laad `js/combobox.js`. Het script geeft het id van de select aan het
  zoekveld (zodat `<label for>` blijft kloppen); zoek de select in andere scripts dus op
  `name`, niet op id.
- **Klassenlijsten** zijn gedeeld (PLAN B60): iedereen bekijkt, gebruikt en kopieert ze; alleen
  de eigenaar en admins hernoemen, vervangen en verwijderen (`policy.CLASS_LIST_MATRIX`). Haal
  een lijst altijd op met `ClassListService.get(db, user, id, ClassListAction.X)`, nooit met
  `db.get` plus een eigen check. Admins zien alle sjablonen (`?toon=alle`, B61) maar beheren
  persoonlijke sjablonen van anderen niet. De presentatieteller telt studenten met een actieve of
  ongeldige inlevering (`handed_in_count`, B62).
- **Toegankelijkheid:** `tests/integration/test_accessibility.py` loopt alle paginatypen af
  (taal, één `h1`, labels, alt, knopnamen). Voeg een nieuwe pagina daar aan toe.
- **CSP:** strikt (`script-src 'self'`, geen inline). Er komen geen inline `<script>` of
  `style=""` in templates. Previews met inline styles draaien in een sandboxed iframe met een
  eigen CSP.

## Conventies

- Python 3.15, en alles vastgepind: exacte versies in `pyproject.toml`, plus `uv.lock`. Images
  pin je op tag én digest, GitHub Actions op commit-SHA.
- ruff (zie `pyproject.toml` voor de regels) en mypy strict, ook voor de tests.
- Tests staan in `tests/unit`, `tests/integration` en `tests/e2e` (marker `e2e`, standaard
  uitgesloten). De database-afhankelijke tests moeten slagen op zowel SQLite als PostgreSQL.
- Commits volgen Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `ci:`).
  Claude maakt de commits per fase (op verzoek van de eigenaar, sinds 14-11-2026). Pushen doet
  de eigenaar, na akkoord.
- Logs: JSON naar stdout, nooit de inhoud van bestanden, en ID's in plaats van namen.
- Er komen geen geheimen, logo's of merkbestanden van de school in de repo.

## Stand van zaken

- [x] Fase 1: fundament
- [x] Fase 2: kern (datamodel, auth, audit, S3-backend)
- [x] Fase 3: examens
- [x] Fase 4: studentflow
- [x] Fase 5: docentkant
- [x] Fase 6: levenscyclus
- [x] Fase 7: afronding
