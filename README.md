# Inleverboks

Met Inleverboks leveren studenten tijdens een examen hun werk digitaal in, zonder account,
via een link en een toegangscode. Docenten maken examens aan, volgen de inleveringen live en
exporteren alles (zip, CSV, proces-verbaal). Ingeleverde bestanden zijn voor studenten nooit
meer in te zien. Alles wat ertoe doet, komt in een auditlog dat niet te wijzigen is.

> **Status:** in ontwikkeling. Fase 1 (fundament), fase 2 (accounts, auth, auditlog) en fase 3
> (examens) zijn klaar. Zie `PLAN.md` voor de fasering.

## Snelstart

### Docker

```bash
docker run -d --name inleverboks \
  -p 8080:8080 -v inleverboks-data:/data \
  -e APP_BASE_URL=http://localhost:8080 \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  -e SESSION_COOKIE_SECURE=false \
  ghcr.io/hoite/inleverboks:edge
```

Open daarna <http://localhost:8080>.

### Docker Compose

```bash
cp .env.example .env        # vul SECRET_KEY in (en voor lokaal: SESSION_COOKIE_SECURE=false)
docker compose up --build
```

Om de opgeschaalde opzet te testen (PostgreSQL en S3/MinIO):

```bash
docker compose -f compose.yaml -f compose.scale.yaml up --build
```

## Eerste start en de admin

1. Zet `ADMIN_EMAIL` (en eventueel `ADMIN_PASSWORD`) en start de app. Bestaat er nog geen admin,
   dan wordt de **local admin** aangemaakt. Is `ADMIN_PASSWORD` leeg, dan genereert de app een
   sterk wachtwoord en toont dat **eenmalig** in de log (`bootstrap.admin_created`).
2. Log in op `/login`. Bij de eerste login moet je het wachtwoord wijzigen en
   **tweestapsverificatie (TOTP)** instellen. TOTP is verplicht voor alle admins. Bewaar de
   herstelcodes goed.
3. Voeg onder *Admin → Whitelist* het domein van de school toe (bijvoorbeeld `alfa-college.nl`)
   of losse e-mailadressen. Alleen wie op de whitelist staat kan worden uitgenodigd en kan
   inloggen. Dat wordt bij **elke** login en elke request opnieuw gecontroleerd. De local admin
   is hiervan vrijgesteld, zodat je jezelf nooit kunt buitensluiten.
4. Nodig docenten uit onder *Admin → Gebruikers*. De app maakt een eenmalige link die 7 dagen
   geldig is. Er wordt geen mail verstuurd: je kopieert de link en stuurt hem zelf door,
   bijvoorbeeld via Teams.
5. Is een docent zijn wachtwoord vergeten, dan maak je bij die gebruiker een **resetlink** (24 uur
   geldig). Zet daarbij eventueel "ook tweestapsverificatie resetten" aan als de telefoon kwijt
   is.

De rechten van de local admin kunnen nooit worden afgenomen. Andere admins kun je aanstellen en
weer afnemen.

**Buitengesloten?** Met de CLI maak je een (extra) admin aan of herstel je er een:

```bash
docker exec -it <container> inleverboks create-admin --email beheer@school.nl
docker exec -it <container> inleverboks create-admin --email beheer@school.nl --reset
```

Het nieuwe wachtwoord wordt eenmalig getoond, en `--reset` wist ook TOTP. Let op: het
TOTP-geheim is versleuteld met een sleutel die is afgeleid van `SECRET_KEY`. Wijzig je
`SECRET_KEY`, dan moet iedereen TOTP opnieuw instellen. Daarnaast vervallen alle sessies.

## Auditlog

Alles wat ertoe doet (logins, uitnodigingen, rolwijzigingen, wijzigingen in de whitelist en de
instellingen, ...) komt in een auditlog dat alleen kan groeien. Elk item bevat de hash van het
vorige item, en de database weigert wijzigingen en verwijderingen. Controleren gaat zo:

```bash
docker exec <container> inleverboks audit verify
```

## Configuratie

Alle instellingen komen uit omgevingsvariabelen (of een K8s Secret/ConfigMap). Zie ook
`.env.example`.

| Variabele | Standaard | Omschrijving |
|---|---|---|
| `APP_BASE_URL` | — (verplicht) | Publieke URL, voor de inleverlinks en de QR-codes |
| `SECRET_KEY` | — (verplicht, ≥ 32 tekens) | Voor sessies en tokens |
| `DATABASE_URL` | `sqlite:////data/inleverboks.db` | SQLite of `postgresql://…` |
| `STORAGE_BACKEND` | `local` | `local` of `s3` |
| `STORAGE_PATH` | `/data/files` | Voor `local` |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_REGION`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | — | Voor `s3` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | — | Bootstrap van de local admin |
| `APP_TIMEZONE` | `Europe/Amsterdam` | Weergave en de telling van de bewaartermijn |
| `TRUSTED_PROXIES` | leeg | CIDR-lijst van proxy's waarvan `X-Forwarded-For`/`-Proto` vertrouwd worden |
| `MAX_FILE_MB`, `MAX_SUBMISSION_MB`, `MAX_FILES_PER_SUBMISSION` | `25`, `100`, `20` | Bovengrenzen |
| `SCHEDULER_ENABLED` | `true` | Achtergrondtaken in de app |
| `RUN_MIGRATIONS_ON_START` | `true` | `false` in k8s (daar doet een initContainer het) |
| `BACKUP_PATH`, `BACKUP_SCHEDULE`, `BACKUP_KEEP_DAYS` | leeg, `0 2 * * *`, `7` | Back-up |
| `SESSION_COOKIE_SECURE` | `true` | `false` alleen voor lokale ontwikkeling via http |
| `HSTS_ENABLED` | `false` | Aanzetten achter TLS |
| `LOG_LEVEL`, `LOG_FORMAT` | `info`, `json` | `LOG_FORMAT=console` voor leesbare logs |
| `DEMO_MODE` | `false` | Toont een demobanner en staat `demo-seed` toe |

## Kubernetes

*(volgt in fase 7: Kustomize-manifests in `deploy/k8s/`, geschikt voor Flux.)*

## Aandachtspunten voor de ingress

*(volgt in fase 7)* In het kort: de maximale request-grootte moet passen bij
`MAX_SUBMISSION_MB`, er zijn ruime timeouts nodig voor uploads en zip-downloads, de ingress
moet forwarded headers doorgeven en TLS regelen, en `TRUSTED_PROXIES` moet op het pod-bereik
van de ingress-controller staan.

## Back-up en herstel

*(volgt in fase 6)*

## Upgraden en migraties

De migraties draaien standaard bij het starten van de container (`RUN_MIGRATIONS_ON_START`).
Handmatig gaat dat zo:

```bash
docker run --rm -v inleverboks-data:/data --env-file .env ghcr.io/hoite/inleverboks:edge inleverboks migrate
```

## Opschalen

*(volgt in fase 7)* Opschalen gaat met PostgreSQL, S3, `replicas > 1` en
`strategy: RollingUpdate`. Let op: de rate limiting (login, TOTP, links, toegangscode, upload)
staat in het geheugen en geldt dus **per pod**. Met 3 replicas is het effectieve maximum
ongeveer 3 keer zo hoog. Redis hiervoor staat op de roadmap.

## Ontwikkelen

Je hebt [uv](https://docs.astral.sh/uv/) en Docker nodig.

```bash
uv sync
uv run pytest                 # tests
uv run pytest -m e2e          # end-to-end (eenmalig: uv run playwright install chromium)
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run uvicorn inleverboks.asgi:app --reload --port 8080
```

Zie `CLAUDE.md` voor de conventies en de architectuur.

## Beveiliging

Een kwetsbaarheid meld je zoals beschreven in `SECURITY.md`. De app draait als non-root
(UID 10001), met een read-only root filesystem, een strikte Content-Security-Policy en zonder
externe scripts of fonts.

## Roadmap

Inloggen via OIDC (Microsoft Entra ID), mail (SMTP), een virusscan (ClamAV), een
importvoorinstelling voor klassenlijsten, examensjablonen delen, Prometheus `/metrics` en
Redis voor rate limiting en sessies.

## Licentie

[EUPL-1.2](LICENSE). De licenties van derden staan in `THIRD_PARTY_NOTICES.md`.
