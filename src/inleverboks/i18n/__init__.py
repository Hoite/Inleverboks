"""Gettext translations. All UI text lives in ``i18n/<locale>/LC_MESSAGES/messages.po``.

The .po files are compiled in memory at startup, so there is no separate build step and
no generated .mo file to keep in sync.
"""

from __future__ import annotations

import io
from functools import cache
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations

DEFAULT_LOCALE = "nl"
I18N_DIR = Path(__file__).parent


@cache
def get_translations(locale: str = DEFAULT_LOCALE) -> Translations:
    po_path = I18N_DIR / locale / "LC_MESSAGES" / "messages.po"
    with po_path.open("rb") as fh:
        catalog = read_po(fh, locale=locale)
    buffer = io.BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    return Translations(buffer, domain="messages")


def gettext(message: str, locale: str = DEFAULT_LOCALE) -> str:
    return get_translations(locale).ugettext(message)
