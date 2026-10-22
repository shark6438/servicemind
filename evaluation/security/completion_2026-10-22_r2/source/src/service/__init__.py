from importlib import import_module

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    if name in __all__:
        return getattr(import_module("service.service"), name)
    raise AttributeError(name)
