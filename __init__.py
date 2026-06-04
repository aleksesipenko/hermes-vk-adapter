def register(ctx):
    from .plugins.vk import register as adapter_register

    return adapter_register(ctx)


__all__ = ["register"]
