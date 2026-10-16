from . import chat, inbox, lma, providers, runtime

__all__ = ["chat", "inbox", "lma", "providers", "runtime"]


def __getattr__(name):
    if name == "lma":
        from . import lma

        return lma
    raise AttributeError(name)


