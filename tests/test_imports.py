"""Every module must import. Scripted refactors have shipped syntax errors in
modules no other test touched; this closes that hole permanently."""

import importlib
import pkgutil

import kodarr


def test_all_modules_import():
    failures = []
    for mod in pkgutil.walk_packages(kodarr.__path__, prefix="kodarr."):
        try:
            importlib.import_module(mod.name)
        except Exception as e:
            failures.append(f"{mod.name}: {e}")
    assert not failures, failures
