"""Composition root shared by API, worker and MCP."""

from eios_runtime.container import BackgroundTasks, Container, build_container

__all__ = ["BackgroundTasks", "Container", "build_container"]
