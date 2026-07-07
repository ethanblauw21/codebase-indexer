"""Feature: import variety (plain / from / aliased) plus calls into imported modules."""
import os
import json as j
from collections import OrderedDict


def build():
    path = os.getcwd()
    data = OrderedDict()
    return j.dumps({"path": path, "data": data})
