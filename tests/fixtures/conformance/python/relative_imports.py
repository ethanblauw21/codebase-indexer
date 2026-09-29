from . import sibling, other as renamed
from .store import load
from ..shared.config import settings
from .. import up
from . import *


def run(key):
    return load(key)
