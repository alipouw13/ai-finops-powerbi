#!/usr/bin/env python3
"""
Conformed dimension & star-schema builder (delegates to unified build_data.py).

Previously `build_data.py` and `build_dimensions.py` were separate scripts that
could overwrite each other's columns (e.g., dropping `persona_key` on
`dim_identity` or `enterprise_discount_pct` on `dim_platform`). Both entry points
now execute the unified FOCUS-native Silver -> Gold build in `build_data.py` so
running either script in any order is 100% idempotent and safe.
"""
from build_data import build

if __name__ == "__main__":
    build()

