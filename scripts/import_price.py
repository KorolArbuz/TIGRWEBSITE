from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shop.database import connect, init_db
from shop.import_service import import_slot
from shop.xlsx_importer import import_xlsx


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт прайса XLSX в каталог")
    parser.add_argument("file", type=Path)
    parser.add_argument("--database", type=Path, default=Path("data/shop.db"))
    parser.add_argument("--media", type=Path, default=Path("data/media"))
    parser.add_argument("--multiplier", type=Decimal, default=Decimal("1.0"))
    parser.add_argument("--deactivate-missing", action="store_true")
    args = parser.parse_args()

    init_db(args.database)
    with import_slot(args.media):
        connection = connect(args.database)
        try:
            result = import_xlsx(
                connection,
                args.file,
                args.media,
                original_filename=args.file.name,
                price_multiplier=args.multiplier,
                deactivate_missing=args.deactivate_missing,
            )
        finally:
            connection.close()
    print(result.as_dict())


if __name__ == "__main__":
    main()
