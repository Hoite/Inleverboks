# Architectuur

## Lagen

```mermaid
flowchart TB
  browser["Browser (docent: Jinja2 + HTMX, student: Jinja2 + ES-modules)"]
  subgraph app["FastAPI-app"]
    mw["Middleware: proxy headers → body-limiet → CSRF-cookie → security headers"]
    web["web/: routes, templates, deps (sessie, rol), CSRF-check"]
    policy["policy.py: autorisatiematrix"]
    services["services/: auth, users, audit, settings, whitelist, totp, ..."]
    adapters["adapters/: StorageBackend · AuthProvider · Mailer · FileScanner · RateLimiter · Clock"]
  end
  db[("SQLite (WAL) / PostgreSQL")]
  store[("Lokale schijf / S3")]
  browser --> mw --> web
  web --> policy
  web --> services
  services --> adapters
  services --> db
  adapters --> store
```

- **web/** kent HTTP, maar heeft geen domeinlogica. Elke route vraagt rechten op via `deps.py`
  (`Teacher`, `Admin`) en `policy.py`.
- **services/** bevat de domeinlogica en heeft geen kennis van HTTP. Fouten zijn `DomainError` met
  een vaste code, en `web/messages.py` vertaalt die codes naar Nederlandse teksten.
- **adapters/** zijn de koppelingen met de buitenwereld. Elke adapter heeft een interface
  (`Protocol`), zodat OIDC, SMTP, ClamAV en Redis later zonder grote verbouwing passen.
- **Clock:** alle tijdafhankelijke code vraagt de tijd aan de `Clock`. Tests gebruiken een
  `FrozenClock` voor tijdvensters, sessies, DST en de bewaartermijn.

## Datamodel

Primaire sleutels zijn UUID v7. Alle tijden zijn timezone-aware en worden in UTC opgeslagen
(`UTCDateTime`). De status van een examen (concept, gepland, open, gesloten) wordt **afgeleid**
en niet opgeslagen. Alleen `finalized_at` (afgerond) staat in de database.

```mermaid
erDiagram
  app_user ||--o{ recovery_code : heeft
  app_user ||--o{ user_session : heeft
  app_user ||--o{ invite : "maakt aan"
  app_user ||--o{ password_reset : "voor"
  app_user ||--o{ exam : "eigenaar"
  app_user |o--o{ exam : "tweede beoordelaar"
  app_user ||--o{ class_list : bezit
  app_user ||--o{ exam_template : bezit
  class_list ||--o{ class_list_entry : bevat
  exam_template ||--o{ exam_template_slot : bevat
  exam_template_slot |o--o| template_file : sjabloon
  exam ||--o{ expected_file : "vakjes"
  expected_file |o--o| template_file : sjabloon
  exam ||--o{ exam_roster_entry : "momentopname klassenlijst"
  exam ||--o{ extension_grant : verlenging
  exam ||--o{ submission : inleveringen
  exam }o--o| ip_preset : "IP-preset"
  submission ||--o{ submission_file : bestanden
  submission_file }o--o| expected_file : "in vakje"
  submission ||--o{ receipt_token : "PDF-token"
  exam ||--o{ proces_verbaal : "PV's"

  app_user {
    uuid id PK
    string email UK
    string password_hash "argon2id"
    string totp_secret_enc "AES-GCM"
    bool is_admin
    bool is_local_admin "nooit af te nemen"
    bool is_active
  }
  user_session {
    uuid id PK
    string token_hash UK "SHA-256 van cookie"
    string stage "mfa | change_password | setup_totp | full"
    datetime last_seen_at "idle 2 uur"
    datetime expires_at "absoluut 12 uur"
  }
  exam {
    uuid id PK
    string slug UK "nooit hergebruikt"
    datetime starts_at
    datetime ends_at
    datetime published_at "null = concept"
    string access_code "leesbaar (B1)"
    bool hold
    datetime retention_delete_after
    datetime finalized_at
  }
  submission {
    uuid id PK
    string student_number
    int attempt
    string status "active | cancelled | invalid"
    string idempotency_key UK
    string receipt_code UK
  }
  submission_file {
    uuid id PK
    string original_name "NFC, zonder pad"
    string storage_key "null na verwijdering"
    string sha256
    json warnings
  }
  audit_event {
    uuid id PK
    string chain "system of exam-id"
    bigint seq
    string prev_hash
    string hash
  }
```

Losse tabellen, zonder relaties: `whitelist_entry`, `setting` (JSON-waarden), `branding_asset`,
`job_lock` (lease voor de scheduler), `retired_slug`, `denied_attempt_bucket` (geweigerde
pogingen per minuut samengevoegd), `audit_event` en `alembic_version`.

**Belangrijke constraints:**
- `uq_submission_one_active`: een partial unique index op `(exam_id, student_number)` met
  `WHERE status = 'active'`. Er is dus maximaal één actieve inlevering per student per examen,
  ook bij gelijktijdige pogingen. Dit werkt op zowel SQLite als PostgreSQL.
- `audit_event (chain, seq)` is uniek, en triggers weigeren `UPDATE` en `DELETE`. Het auditlog
  verwijst bewust niet met een foreign key naar `exam`, zodat het blijft bestaan na een
  definitieve verwijdering.

## Auditlog

```
hash = SHA-256( prev_hash ‖ canonical_json{chain, seq, ts, actor_type, actor_id,
                                           actor_label, ip, event, details} )
```

- Er is een keten per examen en één systeemketen. Het eerste item verwijst naar `"0" * 64`.
- `AuditService.append` schrijft in de transactie van de aanroeper, dus de wijziging en de
  logregel worden samen gecommit. Twee gelijktijdige schrijvers worden achter elkaar gezet door
  de unieke `(chain, seq)`: bij een conflict wordt een savepoint teruggedraaid en volgt een
  nieuwe poging. Op PostgreSQL wacht de tweede schrijver daarnaast via `FOR UPDATE` op de
  laatste rij.
- `inleverboks audit verify` rekent elke keten opnieuw door en meldt per keten de eerste breuk
  (`hash_mismatch`, `broken_link` of `seq_gap`).

## Authenticatie en sessies

```mermaid
sequenceDiagram
  participant B as Browser
  participant A as App
  B->>A: POST /login (e-mail, wachtwoord, CSRF)
  A->>A: rate limit (e-mail en IP) → AuthProvider → actief? whitelist?
  alt TOTP aan
    A-->>B: sessie (stage=mfa, 10 min) → /login/code
    B->>A: POST /login/code (TOTP of herstelcode)
    A->>A: nieuwe sessietoken (rotatie)
  end
  alt eerste login / admin zonder TOTP
    A-->>B: stage=change_password → /account/wachtwoord
    A-->>B: stage=setup_totp → /account/tweestaps → herstelcodes
  end
  A-->>B: stage=full → /docent
```

- De cookie `ib_session` bevat een willekeurige token, met de vlaggen HttpOnly, SameSite=Lax en
  Secure. In de database staat alleen de SHA-256 van die token.
- Bij elke request worden gecontroleerd: intrekking, de absolute duur (12 uur), inactiviteit
  (2 uur), of het account nog actief is en of het adres nog op de whitelist staat. HTMX-polling
  (een element-id dat begint met `poll`) verlengt de sessie niet.
- Veranderen de eisen tijdens een sessie (iemand wordt admin, of TOTP wordt verplicht voor
  iedereen), dan wordt de sessie bij de volgende request teruggezet naar de juiste stap.
- De **local admin** is vrijgesteld van de whitelist. Zo kan de installatie zichzelf nooit
  buitensluiten.
- **CSRF:** een willekeurige waarde in de HttpOnly-cookie `ib_csrf`. Formulieren sturen
  `HMAC(SECRET_KEY, cookie)` mee, scripts sturen die in de header `X-CSRF-Token`. Daarnaast wordt
  de Origin gecontroleerd.

## Autorisatiematrix

Deze matrix staat in `policy.py` en wordt cel voor cel getest in `tests/unit/test_policy.py`.
De afwijking B4 uit het plan is verwerkt: een admin mag ook bij examens van anderen bestanden
downloaden en exporteren.

| Actie | Admin (examen van ander) | Eigenaar | Tweede beoordelaar | Andere docent |
|---|---|---|---|---|
| Instellingen, bestandseisen, tijden, code, IP wijzigen | ✗ | ✓ | ✗ | ✗ |
| Overzicht en metadata van inleveringen | ✓ | ✓ | ✓ | ✗ |
| Bestanden downloaden | ✓ (B4) | ✓ | ✓ | ✗ |
| Proces-verbaal en CSV | ✓ (B4) | ✓ | ✓ | ✗ |
| Auditlog van het examen | ✓ | ✓ | ✓ | ✗ |
| Ongedaan maken, ongeldig verklaren, verlenging | ✗ | ✓ | ✗ | ✗ |
| Bewaren-vlag | ✓ | ✓ | ✗ | ✗ |
| Eigendom overdragen | ✓ | ✗ | ✗ | ✗ |
| Afgerond examen definitief verwijderen | ✓ | ✗ | ✗ | ✗ |
| Ontvangstbewijs controleren | ✓ | ✓ | ✓ | ✗ |
| Concept verwijderen, dupliceren, als sjabloon opslaan | ✗ | ✓ | ✗ | ✗ |

Een admin die zelf eigenaar is van een examen heeft daarop ook alle rechten van een eigenaar.
Studenten hebben geen account. Er bestaat **geen enkele route** waarmee een student ingeleverde
bestanden kan opvragen.

## Studentflow

*(volgt in fase 4: sequence-diagram van openen → code → studentnummer → bestanden →
controle → upload → ontvangstbewijs.)*
