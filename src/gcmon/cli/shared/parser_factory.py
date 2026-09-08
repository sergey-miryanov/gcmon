import argparse
from typing import Protocol


class ParserFactory(Protocol):
    def __call__(self, name: str, *, help: str, description: str) -> argparse.ArgumentParser: ...
