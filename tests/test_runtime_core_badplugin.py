"""A plug-in whose registration fails, for the core process tests. No tests live here."""


def register_core():
    raise RuntimeError("this plug-in is broken on purpose")
