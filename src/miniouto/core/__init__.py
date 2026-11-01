from . import chat, inbox, modelslist, providers, runtime

__all__ = ["chat", "inbox", "modelslist", "providers", "runtime"]


def __getattr__(name):
    if name == "modelslist":
        from . import modelslist

        return modelslist
    raise AttributeError(name)
