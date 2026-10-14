"""Tiny i18n: French source strings are the keys; translations/ar.json and en.json hold the others.
_("Texte {n}", n=3) → translated then formatted. Missing keys fall back to French."""
import json
import os

LANGS = {"fr": "Français", "ar": "العربية", "en": "English"}
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "translations")
_CAT = {}


def load():
    for code in LANGS:
        path = os.path.join(_DIR, f"{code}.json")
        try:
            with open(path, encoding="utf-8") as f:
                _CAT[code] = json.load(f)
        except FileNotFoundError:
            _CAT[code] = {}


load()


def current_lang():
    try:
        from flask import g
        return getattr(g, "lang", "fr")
    except RuntimeError:
        return "fr"


def gettext(s, lang=None, **kw):
    lang = lang or current_lang()
    out = s if lang == "fr" else (_CAT.get(lang, {}).get(s) or s)
    if kw:
        try:
            out = out.format(**kw)
        except (KeyError, IndexError, ValueError):
            out = s.format(**kw)
    return out


class Labels(dict):
    """Status/method labels translated on lookup."""
    def __getitem__(self, k):
        return gettext(dict.__getitem__(self, k))

    def get(self, k, default=None):
        return self[k] if k in self else default
