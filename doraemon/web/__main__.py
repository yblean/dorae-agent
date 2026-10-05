"""python -m doraemon.web   ->   http://127.0.0.1:8000"""
import argparse

import uvicorn

from doraemon.web import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m doraemon.web")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    # 127.0.0.1 only: the page shows your email-derived data and must not be reachable from the network
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
