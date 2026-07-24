"""Command-line entry point for the release builder."""

import argparse
from pathlib import Path

from .checkpoint import load_checkpoint, prepare
from .render import render_report


def build_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--input", required=True)
    prepare_parser.add_argument("--checkpoint", required=True)
    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--checkpoint", required=True)
    complete_parser.add_argument("--output", required=True)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if arguments.command == "prepare":
        prepare(arguments.input, arguments.checkpoint)
    else:
        checkpoint = load_checkpoint(arguments.checkpoint)
        Path(arguments.output).write_text(
            render_report(checkpoint), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
