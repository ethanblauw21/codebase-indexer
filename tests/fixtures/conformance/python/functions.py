"""Feature: top-level functions and the call edges between them."""


def greet(name):
    return format_name(name)


def format_name(name):
    return name.strip().title()


def main():
    greet("ada")
    format_name("grace")
