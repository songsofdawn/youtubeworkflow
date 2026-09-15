"""Hybrid YouTube discovery pipeline with optional local Ollama ranking."""

from .ollama_client import OllamaDiscoveryClient, OllamaDiscoveryError, OllamaSettings

def __getattr__(name):
    # Historical imports remain available without loading the scheduler for Next.
    if name == "DiscoveryPipeline":
        from .pipeline import DiscoveryPipeline
        return DiscoveryPipeline
    if name == "DiscoveryStore":
        from .store import DiscoveryStore
        return DiscoveryStore
    raise AttributeError(name)

__all__ = [
    "DiscoveryPipeline",
    "DiscoveryStore",
    "OllamaDiscoveryClient",
    "OllamaDiscoveryError",
    "OllamaSettings",
]
