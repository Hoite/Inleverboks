# Fase 3: voorgestelde commits

```bash
git add vendor.lock.json scripts/vendor.py src/inleverboks/web/static/vendor/ THIRD_PARTY_NOTICES.md
git commit -m "build: vendor htmx 2.0.11 with pinned checksum"

git add src/inleverboks/services/slugs.py src/inleverboks/services/ipranges.py src/inleverboks/services/timeutil.py src/inleverboks/services/template_files.py src/inleverboks/services/exams.py src/inleverboks/services/classlists.py src/inleverboks/services/container.py src/inleverboks/services/system_settings.py
git commit -m "feat(exams): add exam, template, roster and extension services"

git add src/inleverboks/web/ src/inleverboks/main.py
git commit -m "feat(web): add exam pages, class list import, QR, presentation mode and IP presets"

git add src/inleverboks/i18n/ scripts/i18n_fill.py
git commit -m "feat(i18n): add Dutch translations for exam management"

git add tests/
git commit -m "test: cover exam management, class list import and helpers"

git add README.md CLAUDE.md CHANGELOG.md PLAN.md docs/
git commit -m "docs: document exam management decisions"
```
