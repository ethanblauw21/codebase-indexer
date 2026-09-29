"""ADR-044 resolution fixture: calls bound through this file's imports."""
import json
import ib.tools
from ib.store import load
from ib import tools as t


def loads(text):
    """An in-repo namesake of json.loads."""
    return text


def read(path):
    return json.loads(path)


def fetch(key):
    return load(key)


def build():
    return t.make()


def build_again():
    return ib.tools.make()


def keep(d):
    return d.save()


def local_use():
    return helper()


def helper():
    return 1
