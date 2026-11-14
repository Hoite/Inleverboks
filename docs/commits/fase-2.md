# Fase 2: voorgestelde commits

Voer eerst de commits van fase 1 uit (`docs/commits/fase-1.md`).

```bash
git add src/inleverboks/db/ src/inleverboks/migrations/
git commit -m "feat(db): add full data model, initial migration and append-only audit triggers"

git add src/inleverboks/services/audit.py src/inleverboks/cli.py
git commit -m "feat(audit): add hash-chained audit log with verify command"

git add src/inleverboks/adapters/ src/inleverboks/services/ src/inleverboks/policy.py src/inleverboks/data/
git commit -m "feat(auth): add sessions, TOTP, rate limiting, whitelist, invites and central policy"

git add src/inleverboks/web/ src/inleverboks/main.py src/inleverboks/i18n/ babel.cfg scripts/
git commit -m "feat(web): add login, account and admin screens with CSRF protection"

git add src/inleverboks/adapters/storage/
git commit -m "feat(storage): add S3-compatible storage backend"

git add tests/
git commit -m "test: cover auth flows, audit chain, policy matrix and storage contract"

git add README.md CLAUDE.md CHANGELOG.md PLAN.md docs/ compose.scale.yaml pyproject.toml uv.lock .claude/launch.json
git commit -m "docs: document admin bootstrap, audit log and architecture"
```

Tip: `git add -A && git status` laat zien of er nog iets ontbreekt. `.env` en `data/` worden
genegeerd.
