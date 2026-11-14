# Fase 1: voorgestelde git-commando's

De repo bestaat nog niet. Maak eerst `github.com/hoite/Inleverboks` aan (leeg, zonder README of
licentie). Voer daarna in deze map uit:

```bash
git init -b main
git remote add origin git@github.com:hoite/Inleverboks.git
```

Commits (Conventional Commits, één per logisch onderdeel):

```bash
git add LICENSE SECURITY.md CHANGELOG.md THIRD_PARTY_NOTICES.md README.md CLAUDE.md PLAN.md .gitignore
git commit -m "docs: add license, security policy, readme and project conventions"

git add pyproject.toml uv.lock src/ .env.example
git commit -m "feat: add application skeleton with config, health probes and security headers"

git add tests/
git commit -m "test: add unit, integration and e2e smoke tests for the foundation"

git add Dockerfile .dockerignore compose.yaml compose.scale.yaml
git commit -m "build: add hardened multi-stage Dockerfile and compose files"

git add .github/ docs/
git commit -m "ci: add lint, test matrix, e2e and docker smoke workflow"
```

Pushen gebeurt pas na akkoord:

```bash
git push -u origin main
```

Let op: `inleverboks-claude-code-prompt.md` (de specificatie) staat ook in deze map. Bepaal zelf
of je die mee wilt committen. De `.gitignore` sluit `.claude/settings.local.json` uit.
