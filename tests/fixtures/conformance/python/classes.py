"""Feature: a class with __init__ and methods (owns edges, method kinds)."""


class Counter:
    def __init__(self, start):
        self.value = start

    def increment(self):
        self.value += 1
        return self.value

    def reset(self):
        self.value = 0
