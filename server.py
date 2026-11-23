"""Keep existing client commands that launch this file working."""

from pathlib import Path

from telegram_mcp.cli import main

if __name__ == "__main__":
    main(legacy_root=Path(__file__).resolve().parent)
