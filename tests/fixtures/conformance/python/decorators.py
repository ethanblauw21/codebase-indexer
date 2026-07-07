"""Feature: decorated functions and methods — decoration must not drop the symbol."""
import functools


def trace(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


class Service:
    @property
    def name(self):
        return self._name

    @staticmethod
    def version():
        return "1.0"


@trace
def compute(x):
    return x * 2
