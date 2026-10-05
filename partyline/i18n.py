import gettext
import locale
import os

LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
DOMAIN = "partyline"
LANGUAGES = (
    ("", "System default"),
    ("en", "English"),
    ("nl", "Nederlands"),
    ("eo", "Esperanto"),
    ("es", "Español"),
    ("ru", "Русский"),
)
translation = gettext.NullTranslations()


def system_language():
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(variable)
        if value:
            return value.split(":")[0]
    try:
        return locale.getlocale()[0] or ""
    except (TypeError, ValueError):
        return ""


def install(language=None):
    global translation
    language = language or system_language()
    languages = [language] if language else []
    translation = gettext.translation(DOMAIN, LOCALE_DIR, languages=languages, fallback=True)
    return translation


def _(text):
    return translation.gettext(text)


def ngettext(singular, plural, number):
    return translation.ngettext(singular, plural, number)


def N_(text):
    return text
