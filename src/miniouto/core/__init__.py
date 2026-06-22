from . import chat, lma, providers, runtime

__all__ = ["chat", "lma", "providers", "runtime"]


def __getattr__(name):
    if name == "lma":
        from . import lma

        return lma
    raise AttributeError(name)


