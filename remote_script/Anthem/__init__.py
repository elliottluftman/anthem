def create_instance(c_instance):
    """Entry point Live looks for. Imported lazily so the pure modules are testable without Live."""
    from .anthem import Anthem
    return Anthem(c_instance)
