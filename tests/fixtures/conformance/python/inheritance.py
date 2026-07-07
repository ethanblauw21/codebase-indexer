"""Feature: subclassing — a correct extractor emits an `extends` edge Derived -> Base."""


class Animal:
    def speak(self):
        return "..."


class Dog(Animal):
    def speak(self):
        return "woof"
