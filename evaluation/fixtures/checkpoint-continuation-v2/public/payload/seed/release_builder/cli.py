"""Command-line entry point for the release builder."""

import argparse


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
    build_parser().parse_args(argv)
    raise NotImplementedError


if __name__ == "__main__":
    main()
