# Inleverboks

Met Inleverboks leveren studenten tijdens een examen hun werk digitaal in, zonder account,
via een link en een toegangscode. Docenten maken examens aan, volgen de inleveringen live en
exporteren alles (zip, CSV, proces-verbaal). Ingeleverde bestanden zijn voor studenten nooit
meer in te zien. Alles wat ertoe doet, komt in een auditlog dat niet te wijzigen is.

> **Status:** alle fasen uit `PLAN.md` zijn gebouwd. Zie `CHANGELOG.md` voor wat erin zit.

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

## Inleveren (studenten)

Studenten openen de inleverlink (of scannen de QR-code), vullen de toegangscode en hun
studentnummer in, kiezen hun bestanden, controleren ze (naam, vakje, grootte, SHA-256 en een
voorbeeld) en leveren in. Ze krijgen een ontvangstbewijs met een inlevercode en een
verificatiecode. Ingeleverde bestanden zijn voor studenten nooit meer in te zien.

**HTTPS is nodig.** De browser berekent de SHA-256 met de Web Crypto API, en die werkt alleen in
een beveiligde context (HTTPS, of `localhost` tijdens het ontwikkelen).

## Docenten

Op de examenpagina zien docenten live wie heeft ingeleverd, met signalen zoals "leeg sjabloon"
of "identiek aan een andere student". Ze kunnen een inlevering ongedaan maken of ongeldig
verklaren (met een reden), alles downloaden als zip (met `manifest.csv` en een
integriteitscontrole), het overzicht als CSV exporteren, een proces-verbaal (PDF) maken en een
ontvangstbewijs van een student controleren. Elke download en export komt in het auditlog.

**Examensjablonen** (tabblad *Examensjablonen*) bewaren de verwachte bestanden,
sjabloonbestanden, extensies, limieten en toegangsinstellingen. Je maakt ze daar zelf, of slaat
een bestaand examen op als sjabloon. Bij **Nieuw examen** kies je bovenaan een sjabloon. Tijden,
linknaam en klassenlijst kies je altijd per examen. Sjablonen zijn persoonlijk en hebben een
eigen (paarse) kleur, zodat je ze niet met een examen verwart.

**Sjablonen uit een kwalificatie:** onder *Examensjablonen → Sjabloon uit crebo* zoek je een
opleiding op crebonummer of naam van de uitstroom (bijvoorbeeld 27016). Kies de kerntaken: elke
kerntaak wordt een sjabloon met een vakje per werkproces, zoals `B1-K1-W1 [naam]`. De
sjabloonbestanden upload je daarna zelf. Een admin kan sjablonen **delen** met alle docenten;
alleen admins beheren gedeelde sjablonen. Bij een nieuw examen kies je welke werkprocessen
meedoen (een **deelexamen**). Vanuit het overzicht maak je met één klik een **herkansing** voor
een of meer studenten, met alleen de werkprocessen die voor hen nog openstaan. De kwalificaties
komen uit de kwalificatiestructuur van SBB; het inlezen daarvan volgt.

## Huisstijl

Onder *Admin → Huisstijl* stelt de admin de naam van de app, het lettertype (Poppins of het
systeemlettertype), de kleuren en het logo in. Een live voorbeeld en een contrastcontrole
(WCAG AA: 4,5:1 voor tekst, 3:1 voor accentlijnen) laten direct zien of alles leesbaar blijft.
Bij te weinig contrast verschijnt een waarschuwing; opslaan kan dan nog wel. Met **Terug naar
standaard** zet je naam, kleuren en lettertype terug (het logo blijft staan).

Het logo is een PNG of WebP van maximaal 1 MB. De app leest het in, verkleint het tot maximaal
512 × 512 pixels en slaat het opnieuw op als PNG. Zo blijven er geen metadata of verborgen
inhoud achter. Het favicon wordt van het logo afgeleid. SVG wordt niet ondersteund. De repo
bevat geen logo's of merkbestanden van de school, alleen een neutraal standaardlogo.

De standaardkleuren komen uit de brandguide van Alfa-college (zie `PLAN.md` §2a). Poppins
(SIL Open Font License) wordt door de app zelf geleverd, zonder Google Fonts of CDN. Met
meerdere replicas zien de andere pods een wijziging binnen 30 seconden.

## Demo

Met `DEMO_MODE=true` toont de app een duidelijke demobanner, en vult `demo-seed` een lege
installatie met voorbeelden: een demodocent (`docent@demo.test`), een gesloten, een open, een
gepland en een conceptexamen, en een paar fictieve inleveringen met signalen. Het wachtwoord
van de demodocent wordt eenmalig getoond.

```bash
docker exec inleverboks inleverboks demo-seed
```

Zonder `DEMO_MODE=true` weigert het commando. Gebruik een demo-installatie nooit voor echte
examens.

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

In `deploy/k8s/` staan Kustomize-manifesten die ook met Flux werken:

```
deploy/k8s/
  base/                 Deployment (1 replica, Recreate), Service, PVC (10Gi), ConfigMap
  overlays/test/        namespace, image-tag, config-patch en secretGenerator
```

De pod draait als UID 10001 met een read-only root filesystem, zonder capabilities en met
seccomp `RuntimeDefault`, en voldoet daarmee aan de Pod Security Standard *restricted* (de
namespace van het overlay dwingt dat af). Een initContainer voert de migraties uit;
`/tmp` (waar uploads tijdelijk staan) is een `emptyDir` van maximaal 2Gi. Resources: requests
100m/256Mi, limits 1 CPU/512Mi (WeasyPrint heeft geheugen nodig voor PDF's).

```bash
cd deploy/k8s/overlays/test
cp secret.env.example secret.env     # SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD (gitignored)
# pas in kustomization.yaml de hostnaam (APP_BASE_URL), TRUSTED_PROXIES en de image-tag aan
kubectl apply -k .
```

Met Flux maak je het secret liever met SOPS of een Sealed Secret aan, in plaats van
`secret.env`. Er zit bewust geen Ingress-manifest bij; zie hieronder wat de ingress moet regelen.

### Images

Elke push naar `main` publiceert `ghcr.io/hoite/inleverboks:edge` en `:sha-<commit>`; een tag
`v1.2.3` publiceert `:1.2.3` en `:1.2`. De images zijn multi-arch (amd64 en arm64) en hebben een
SBOM en provenance. Trivy scant elk image vóór het pushen: bij een CRITICAL kwetsbaarheid wordt
er niets gepubliceerd, en de resultaten staan onder *Security → Code scanning*. Pin in productie
een versie of een `sha-…`-tag in plaats van `edge`.

## Aandachtspunten voor de ingress

- **Request-grootte:** minimaal `MAX_SUBMISSION_MB` plus ongeveer 1 MB voor de
  multipart-overhead. Bij ingress-nginx: `nginx.ingress.kubernetes.io/proxy-body-size: "101m"`.
  De app weigert zelf alles wat groter is.
- **Timeouts:** ruim, voor trage uploads en grote zip-downloads, bijvoorbeeld 600 seconden
  (`proxy-read-timeout` en `proxy-send-timeout`).
- **Buffering:** zet request-buffering uit (`proxy-request-buffering: "off"`), zodat uploads
  direct naar de app streamen. Response-buffering mag ook uit, voor de gestreamde zip.
- **Forwarded headers:** de ingress moet `X-Forwarded-For` en `X-Forwarded-Proto` doorgeven, en
  `TRUSTED_PROXIES` moet het pod-bereik van de ingress-controller bevatten (bijvoorbeeld
  `10.42.0.0/16`). Zonder die instelling ziet de app alleen het IP-adres van de ingress, en werkt
  de IP-whitelist van examens niet. Komt het verkeer via nog een proxy of load balancer, zet die
  dan ook in de lijst.
- **TLS:** verplicht. De browser berekent de hashes alleen in een beveiligde context, en
  cookies zijn standaard `Secure`. Zet daarna `HSTS_ENABLED=true`.

## Bewaartermijn en afronden

Na het einde van een examen (inclusief de laatste verlenging) loopt de bewaartermijn die de
admin instelt (standaard 60 dagen). Die telt in hele kalenderdagen in `APP_TIMEZONE`. Eindigt
een examen op vrijdag 13 november 2026, dan worden de bestanden verwijderd op 13 januari 2027
om 00:00. Vanaf 7 dagen ervoor tonen het dashboard en het overzicht een waarschuwing.

Bij het verwijderen maakt de app eerst een **definitief proces-verbaal**, met een
integriteitscontrole. Daarna verwijdert hij alle ingeleverde bestanden (ook geannuleerde en
ongeldige pogingen) en de klassenlijst van het examen. Het examen staat daarna onder
**Afgerond**. De metadata, de proces-verbalen en het auditlog blijven bewaard.

- **Bewaren-vlag:** de eigenaar of de admin kan de verwijdering met een reden tegenhouden,
  bijvoorbeeld bij een bezwaar. Wordt de vlag weggehaald terwijl de termijn al verstreken is,
  dan volgt de verwijdering op zijn vroegst 7 dagen later.
- **Definitief verwijderen:** alleen de admin kan een afgerond examen helemaal verwijderen, met
  een reden. Dat komt in het systeemlog, en de link (slug) wordt nooit hergebruikt.

De achtergrondtaken (bewaartermijn elk kwartier, opruimen elk uur, back-up volgens
`BACKUP_SCHEDULE`) draaien in de app zelf (`SCHEDULER_ENABLED`). Met meerdere replicas
voorkomt een lock in de database dat een taak dubbel draait. Je kunt ze ook met de hand
starten:

```bash
inleverboks retention run --dry-run   # laat zien welke examens aan de beurt zijn
inleverboks retention run
inleverboks cleanup run               # verweesde bestanden, verlopen tokens en sessies
inleverboks integrity check [--exam ID]   # hashes van alle bestanden controleren (exitcode 1 bij een afwijking)
```

## Back-up en herstel

Met `BACKUP_PATH` maakt de app elke nacht (`BACKUP_SCHEDULE`, in `APP_TIMEZONE`) een archief
`inleverboks-backup-JJJJMMDD-UUMMSS.tar.gz`. Archieven ouder dan `BACKUP_KEEP_DAYS` worden
opgeruimd. Het archief bevat:

- `files/…`: alle opgeslagen bestanden (ingeleverde bestanden, sjablonen en proces-verbalen);
- `database/inleverboks.db`: een consistente kopie van de SQLite-database (via de backup-API
  van SQLite, dus veilig terwijl de app draait);
- `backup.json`: de versie en het tijdstip.

**PostgreSQL** zit niet in het archief. Maak daarvoor een eigen back-up met `pg_dump` (of de
back-upvoorziening van je database). In het archief staat dan alleen een `README.txt` met die
uitleg. Gebruik je S3, dan kun je in plaats daarvan ook versiebeheer op de bucket gebruiken.

Handmatig:

```bash
docker exec inleverboks inleverboks backup --to /data/backups --keep-days 7
```

Herstellen doe je met de app **gestopt**:

```bash
docker run --rm -v inleverboks-data:/data --env-file .env ghcr.io/hoite/inleverboks:edge \
  inleverboks restore /data/backups/inleverboks-backup-20270113-020000.tar.gz --yes
```

De bestaande SQLite-database blijft bewaard als `inleverboks.db.before-restore`. Controleer
daarna de hash-keten en de bestanden:

```bash
inleverboks audit verify
inleverboks integrity check
```

Een back-up bevat ingeleverd werk en persoonsgegevens. Bewaar hem dus even zorgvuldig als de
app zelf, en houd bij het bewaren rekening met de bewaartermijn.

## Upgraden en migraties

De migraties draaien standaard bij het starten van de container (`RUN_MIGRATIONS_ON_START`).
Handmatig gaat dat zo:

```bash
docker run --rm -v inleverboks-data:/data --env-file .env ghcr.io/hoite/inleverboks:edge inleverboks migrate
```

## Opschalen

Voor meer dan één replica:

1. **PostgreSQL** in plaats van SQLite (`DATABASE_URL=postgresql://…`).
2. **S3** (bijvoorbeeld MinIO) in plaats van lokale opslag (`STORAGE_BACKEND=s3`), en geen PVC
   meer voor `/data`.
3. `replicas > 1` en `strategy: RollingUpdate` in een overlay.

De sessies staan in de database, en de achtergrondtaken gebruiken een lock in de database,
zodat ze maar op één pod tegelijk draaien. Kanttekeningen:

- De **rate limiting** (login, TOTP, links, toegangscode, upload) staat in het geheugen en geldt
  dus **per pod**. Met 3 replicas is het effectieve maximum ongeveer 3 keer zo hoog. Redis
  hiervoor staat op de roadmap.
- Een wijziging in de **huisstijl** is op de andere pods binnen 30 seconden zichtbaar.
- De ingebouwde **back-up** bevat bij PostgreSQL alleen de bestanden; maak daarnaast een
  `pg_dump`.

`compose.scale.yaml` start PostgreSQL en MinIO om deze opzet lokaal te testen.

## Ontwikkelen

Je hebt [uv](https://docs.astral.sh/uv/) en Docker nodig. Voor PDF's (WeasyPrint) lokaal ook
Pango: `brew install pango` op macOS, plus `export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib`
(bijvoorbeeld in `~/.zprofile`), zodat Python de bibliotheken vindt.

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
