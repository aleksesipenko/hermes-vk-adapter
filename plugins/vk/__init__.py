def register(ctx):
    from .adapter import register as adapter_register

    return adapter_register(ctx)

__all__ = ["register"]
