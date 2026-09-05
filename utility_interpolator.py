#!/usr/bin/env python3
from __future__ import annotations

import sys
from typing import Sequence

import gas_usage_interpolator
import water_usage_interpolator


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('Usage: utility_interpolator.py {gas|water} [options]')
        print('Use gas --help or water --help for utility-specific arguments.')
        return 0 if args else 2
    utility, *remaining = args
    if utility == 'gas':
        return gas_usage_interpolator.main(remaining)
    if utility == 'water':
        return water_usage_interpolator.main(remaining)
    raise ValueError(f'unknown utility: {utility}')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
