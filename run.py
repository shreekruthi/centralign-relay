"""Beginner launcher: python run.py starts the whole standalone application."""
import sys
from operator_app.__main__ import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    main()
