"""Workarounds and helpers for Esri/arcgis-python-api#2537."""

from .fix_viewmanager_create_2537 import apply, unapply

__all__ = ["apply", "unapply"]
