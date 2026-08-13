"""Workarounds and helpers for Esri/arcgis-python-api#2536."""

from .fix_copy_flc_relationships import apply, restore

__all__ = ["apply", "restore"]
