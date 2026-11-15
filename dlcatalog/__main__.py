"""Command-line entry point: ``python -m dlcatalog <command>``.

Commands
--------
generate-fixtures DIR [--port N]   write the IIIF fixture corpus
fixture-server DIR [--host --port]  serve fixtures with control API
harvest SEED --db PATH [--mode]     run a crawl / revision check on the CLI
app --db PATH --export-dir DIR      run the catalog web application
export --db PATH --out DIR          write a catalog JSON export
"""
from __future__ import annotations

import argparse
import json
import sys

from .export import export_json
from .fixture.generate import generate
from .fixture.server import serve as serve_fixtures
from .harvester import Harvester, MODE_CRAWL, MODE_REFETCH, MODE_REFETCH_NODE
from .server.app import serve as serve_app
from .store import Store


def _cmd_generate(args: argparse.Namespace) -> int:
    manifest = generate(args.dir, port=args.port)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def _cmd_fixture_server(args: argparse.Namespace) -> int:
    serve_fixtures(args.dir, host=args.host, port=args.port,
                   verbose=args.verbose)
    return 0


def _cmd_harvest(args: argparse.Namespace) -> int:
    store = Store(args.db)
    harvester = Harvester(store)
    if args.mode == MODE_REFETCH_NODE:
        run_id = harvester.refetch_node(args.seed)
    else:
        run_id = harvester.run(args.seed, mode=args.mode)
    run = store.get_run(run_id)
    print(json.dumps(dict(run), ensure_ascii=False, indent=2))
    store.close()
    return 0 if run["status"] == "done" else 1


def _cmd_app(args: argparse.Namespace) -> int:
    serve_app(args.db, args.export_dir, host=args.host, port=args.port,
              verbose=args.verbose)
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    store = Store(args.db)
    path = export_json(store, args.out, seed_url=args.seed)
    store.close()
    print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dlcatalog",
                                     description="IIIF collection catalog")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-fixtures", help="create IIIF fixture docs")
    gen.add_argument("dir")
    gen.add_argument("--port", type=int, default=8090)
    gen.set_defaults(func=_cmd_generate)

    fix = sub.add_parser("fixture-server", help="run the local fixture server")
    fix.add_argument("dir")
    fix.add_argument("--host", default="127.0.0.1")
    fix.add_argument("--port", type=int, default=8090)
    fix.add_argument("--verbose", action="store_true")
    fix.set_defaults(func=_cmd_fixture_server)

    har = sub.add_parser("harvest", help="run a harvest on the CLI")
    har.add_argument("seed")
    har.add_argument("--db", required=True)
    har.add_argument("--mode", choices=[MODE_CRAWL, MODE_REFETCH,
                                        MODE_REFETCH_NODE],
                     default=MODE_CRAWL)
    har.set_defaults(func=_cmd_harvest)

    app = sub.add_parser("app", help="run the catalog web application")
    app.add_argument("--db", required=True)
    app.add_argument("--export-dir", required=True)
    app.add_argument("--host", default="127.0.0.1")
    app.add_argument("--port", type=int, default=8000)
    app.add_argument("--verbose", action="store_true")
    app.set_defaults(func=_cmd_app)

    exp = sub.add_parser("export", help="export catalog JSON with paths")
    exp.add_argument("--db", required=True)
    exp.add_argument("--out", required=True)
    exp.add_argument("--seed", default=None)
    exp.set_defaults(func=_cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
