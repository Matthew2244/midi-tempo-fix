import sys

from .cli import run

if __name__ == "__main__":  # worker processes re-import this module; only the real run does the work
    sys.exit(run())
