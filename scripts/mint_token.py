"""Mint a dev JWT for the agent API.

    AUTH_SECRET=... python -m scripts.mint_token --user u_1001 [--ttl 3600]

Prints a Bearer token. HS256, with sub=<user> and exp. Dev use only.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import jwt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="u_1001", help="user_id placed in the token's sub claim")
    parser.add_argument("--ttl", type=int, default=3600, help="seconds until the token expires")
    parser.add_argument("--secret", default=os.environ.get("AUTH_SECRET"), help="HS256 secret (or AUTH_SECRET)")
    args = parser.parse_args(argv)
    if not args.secret:
        print("set AUTH_SECRET or pass --secret", file=sys.stderr)
        return 2
    token = jwt.encode({"sub": args.user, "exp": int(time.time()) + args.ttl}, args.secret, algorithm="HS256")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
