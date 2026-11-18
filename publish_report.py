#!/usr/bin/env python3
"""Compatibility entry point for the single validated report publisher."""
from pathlib import Path

from bank_daily_setup.publish_report import main


if __name__ == '__main__':
    main(default_site_dir=Path(__file__).resolve().parent)
