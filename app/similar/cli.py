"""Замер памяти: python -m app.similar.cli memory"""

import argparse
import resource
import sys

from app.similar.embeddings import embed, get_model


def peak_rss_mb() -> float:
    """Пиковая память процесса (RSS), МБ. Linux отдаёт КБ, macOS — байты."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["memory"])
    parser.parse_args()

    get_model()
    embed("Warm-up. Shooter. A short text to run the model once.")
    print(f"peak_rss_mb={peak_rss_mb():.0f}")


if __name__ == "__main__":
    main()
