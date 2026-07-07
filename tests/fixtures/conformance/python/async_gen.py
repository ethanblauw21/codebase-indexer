"""Feature: async functions and generators — both are first-class symbols with calls."""
import asyncio


async def fetch(url):
    await asyncio.sleep(0)
    return url


def counter(n):
    for i in range(n):
        yield i


async def main():
    await fetch("http://x")
    list(counter(3))
