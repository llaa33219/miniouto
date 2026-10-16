"""`python -m miniouto` — the same entrypoint as the `miniouto` console script."""

from .cli import app

if __name__ == "__main__":
    app()
