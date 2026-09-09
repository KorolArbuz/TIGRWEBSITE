from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def rels_path(part_path: str) -> str:
    directory, filename = posixpath.split(part_path)
    return posixpath.join(directory, "_rels", filename + ".rels")


def resolve_target(part_path: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(part_path), target))


def parse_relationships(zf: zipfile.ZipFile, part_path: str) -> dict[str, str]:
    rp = rels_path(part_path)
    if rp not in zf.namelist():
        return {}
    root = ET.fromstring(zf.read(rp))
    out: dict[str, str] = {}
    for rel in root.findall(q(NS_REL_PKG, "Relationship")):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            out[rid] = resolve_target(part_path, target)
    return out


def get_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join((t.text or "") for t in node.iter(q(NS_MAIN, "t")))


def read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(path))
    return [get_text(si) for si in root.findall(q(NS_MAIN, "si"))]


def col_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 0
    value = 0
    for ch in letters.group(0):
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def read_cell_value(cell: ET.Element, shared: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return get_text(cell.find(q(NS_MAIN, "is")))
    value_node = cell.find(q(NS_MAIN, "v"))
    raw = value_node.text if value_node is not None else None
    if raw is None:
        return None
    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return raw
    if cell_type == "b":
        return raw == "1"
    return raw


def read_sheet_rows(zf: zipfile.ZipFile, sheet_path: str, shared: list[str]) -> dict[int, dict[int, Any]]:
    root = ET.fromstring(zf.read(sheet_path))
    sheet_data = root.find(q(NS_MAIN, "sheetData"))
    rows: dict[int, dict[int, Any]] = {}
    if sheet_data is None:
        return rows
    for row in sheet_data.findall(q(NS_MAIN, "row")):
        try:
            row_num = int(row.attrib.get("r", "0"))
        except ValueError:
            continue
        values: dict[int, Any] = {}
        for cell in row.findall(q(NS_MAIN, "c")):
            ref = cell.attrib.get("r", "A1")
            values[col_index(ref)] = read_cell_value(cell, shared)
        rows[row_num] = values
    return rows


def drawing_for_sheet(zf: zipfile.ZipFile, sheet_path: str) -> str | None:
    root = ET.fromstring(zf.read(sheet_path))
    drawing = root.find(q(NS_MAIN, "drawing"))
    if drawing is None:
        return None
    rid = drawing.attrib.get(q(NS_REL_DOC, "id"))
    if not rid:
        return None
    return parse_relationships(zf, sheet_path).get(rid)


def read_image_anchors(zf: zipfile.ZipFile, sheet_path: str) -> dict[int, list[tuple[int, int, str]]]:
    drawing_path = drawing_for_sheet(zf, sheet_path)
    if not drawing_path or drawing_path not in zf.namelist():
        return {}
    rels = parse_relationships(zf, drawing_path)
    root = ET.fromstring(zf.read(drawing_path))
    by_row: dict[int, list[tuple[int, int, str]]] = {}
    for anchor_tag in ("oneCellAnchor", "twoCellAnchor"):
        for anchor in root.findall(q(NS_XDR, anchor_tag)):
            pic = anchor.find(q(NS_XDR, "pic"))
            if pic is None:
                continue
            from_node = anchor.find(q(NS_XDR, "from"))
            if from_node is None:
                continue
            row_node = from_node.find(q(NS_XDR, "row"))
            col_node = from_node.find(q(NS_XDR, "col"))
            if row_node is None or col_node is None:
                continue
            try:
                row_num = int(row_node.text or "0") + 1
                col_num = int(col_node.text or "0")
            except ValueError:
                continue
            col_off_node = from_node.find(q(NS_XDR, "colOff"))
            try:
                col_off = int(col_off_node.text or "0") if col_off_node is not None else 0
            except ValueError:
                col_off = 0
            blip = pic.find(f".//{q(NS_A, 'blip')}")
            if blip is None:
                continue
            rid = blip.attrib.get(q(NS_REL_DOC, "embed"))
            media_path = rels.get(rid or "")
            if not media_path or media_path not in zf.namelist():
                continue
            by_row.setdefault(row_num, []).append((col_num, col_off, media_path))
    for row_num, items in by_row.items():
        items.sort(key=lambda item: (item[0], item[1], item[2]))
    return by_row


def normalize_header(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = re.sub(r"[\s\n\r\t._\-:/\\]+", "", text)
    return text

HEADER_ALIASES = {
    "model": ("модель", "наименование", "названиетовара", "товар"),
    "color": ("цвет",),
    "barcode": ("штрихкод", "barcode", "sku", "артикул"),
    "box_qty": ("штвкор", "штвкоробке", "количествовкоробке", "упаковка"),
    "price": ("цена", "оптоваяцена", "стоимость"),
    "description": ("описание", "характеристики"),
    "comment": ("комментарий", "примечание", "метка"),
    "category": ("категория", "группа", "раздел"),
}


def match_header(value: Any) -> str | None:
    normalized = normalize_header(value)
    if not normalized:
        return None
    for field, aliases in HEADER_ALIASES.items():
        if normalized in aliases or any(normalized.startswith(alias) for alias in aliases):
            return field
    return None


def detect_header(rows: dict[int, dict[int, Any]]) -> tuple[int, dict[str, int]]:
    best: tuple[int, int, dict[str, int]] | None = None
    for row_num in sorted(rows)[:20]:
        mapping: dict[str, int] = {}
        for col, value in rows[row_num].items():
            field = match_header(value)
            if field and field not in mapping:
                mapping[field] = col
        score = sum(field in mapping for field in ("model", "barcode", "price", "color", "description"))
        if best is None or score > best[0]:
            best = (score, row_num, mapping)
    if not best or best[0] < 3:
        raise ValueError("Не удалось определить строку заголовков: нужны как минимум модель, штрихкод и цена")
    mapping = dict(best[2])
    # В прайсах HOCO/Borofone первая колонка — категория, но заголовок пустой.
    if "category" not in mapping:
        model_col = mapping.get("model", 1)
        if model_col > 0:
            mapping["category"] = model_col - 1
    return best[1], mapping


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def normalize_title_text(value: Any) -> str:
    return re.sub(r"\s+", " ", normalize_text(value)).strip()


def normalize_barcode(value: Any) -> str:
    text = normalize_title_text(value)
    if not text:
        return ""
    # Preserve leading zeroes for text barcodes. Convert only decimal/scientific
    # numeric representations produced by spreadsheet software.
    if not re.fullmatch(r"\d+", text):
        try:
            decimal = Decimal(text.replace(",", "."))
            if decimal == decimal.to_integral_value():
                text = format(decimal.quantize(Decimal("1")), "f")
        except InvalidOperation:
            pass
    digits = re.sub(r"\D", "", text)
    return digits if 6 <= len(digits) <= 18 else ""


def parse_price_cents(value: Any) -> int | None:
    text = normalize_title_text(value).replace(" ", "").replace("₽", "").replace("руб.", "").replace("руб", "")
    text = text.replace(",", ".")
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0))
    except InvalidOperation:
        return None
    if amount <= 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class ParsedProduct:
    brand: str
    category: str
    model: str
    color: str
    barcode: str
    box_qty: str
    price_cents: int
    description: str
    badge: str
    sheet_name: str
    row_number: int
    media_paths: list[str]


def parse_workbook(path: str | Path) -> tuple[list[ParsedProduct], dict[str, bytes], list[str]]:
    products: list[ParsedProduct] = []
    media: dict[str, bytes] = {}
    warnings: list[str] = []
    with zipfile.ZipFile(path) as zf:
        shared = read_shared_strings(zf)
        workbook_path = "xl/workbook.xml"
        root = ET.fromstring(zf.read(workbook_path))
        workbook_rels = parse_relationships(zf, workbook_path)
        sheets_node = root.find(q(NS_MAIN, "sheets"))
        if sheets_node is None:
            return products, media, ["В книге нет листов"]
        for sheet in sheets_node.findall(q(NS_MAIN, "sheet")):
            sheet_name = sheet.attrib.get("name", "Лист")
            rid = sheet.attrib.get(q(NS_REL_DOC, "id"))
            sheet_path = workbook_rels.get(rid or "")
            if not sheet_path or sheet_path not in zf.namelist():
                warnings.append(f"{sheet_name}: XML листа не найден")
                continue
            rows = read_sheet_rows(zf, sheet_path, shared)
            try:
                header_row, columns = detect_header(rows)
            except ValueError as exc:
                warnings.append(f"{sheet_name}: {exc}")
                continue
            images_by_row = read_image_anchors(zf, sheet_path)
            brand = normalize_title_text(sheet_name)
            for row_num in sorted(rows):
                if row_num <= header_row:
                    continue
                row = rows[row_num]
                model = normalize_title_text(row.get(columns["model"]))
                barcode = normalize_barcode(row.get(columns["barcode"]))
                price_cents = parse_price_cents(row.get(columns["price"]))
                if not (model and barcode and price_cents):
                    # Ignore truly empty rows; report only rows that look like products.
                    if model or barcode or price_cents:
                        warnings.append(f"{sheet_name}, строка {row_num}: пропущена (нужны модель, штрихкод и цена)")
                    continue
                item_media = [m for _col, _off, m in images_by_row.get(row_num, [])]
                dedup_media: list[str] = []
                seen_hashes: set[str] = set()
                for media_path in item_media:
                    blob = zf.read(media_path)
                    digest = hashlib.sha256(blob).hexdigest()
                    if digest in seen_hashes:
                        continue
                    seen_hashes.add(digest)
                    dedup_media.append(media_path)
                    media.setdefault(media_path, blob)
                comment = normalize_text(row.get(columns.get("comment", -1))) if "comment" in columns else ""
                badge = "Новинка" if "новин" in comment.lower() else comment
                products.append(ParsedProduct(
                    brand=brand,
                    category=normalize_title_text(row.get(columns.get("category", -1))),
                    model=model,
                    color=normalize_title_text(row.get(columns.get("color", -1))),
                    barcode=barcode,
                    box_qty=normalize_title_text(row.get(columns.get("box_qty", -1))),
                    price_cents=price_cents,
                    description=normalize_text(row.get(columns.get("description", -1))),
                    badge=badge,
                    sheet_name=sheet_name,
                    row_number=row_num,
                    media_paths=dedup_media,
                ))
    return products, media, warnings


@dataclass
class ImportResult:
    filename: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    deactivated: int = 0
    skipped: int = 0
    image_count: int = 0
    warnings: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deactivated": self.deactivated,
            "skipped": self.skipped,
            "image_count": self.image_count,
            "warnings": self.warnings or [],
        }


def _verify_archive(path: str | Path, max_uncompressed_bytes: int = 300 * 1024 * 1024) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            if len(zf.infolist()) > 10_000:
                raise ValueError("В XLSX слишком много внутренних файлов")
            total_size = sum(info.file_size for info in zf.infolist())
            if total_size > max_uncompressed_bytes:
                raise ValueError("Распакованный XLSX превышает безопасный лимит")
            if "xl/workbook.xml" not in zf.namelist():
                raise ValueError("Файл не похож на корректную книгу XLSX")
    except zipfile.BadZipFile as exc:
        raise ValueError("Файл повреждён или не является XLSX") from exc


def _image_extension(media_path: str, blob: bytes) -> str:
    suffix = Path(media_path).suffix.lower()
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return ".webp"
    return suffix if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else ".bin"


def _save_image(blob: bytes, media_path: str, upload_root: str | Path) -> str:
    digest = hashlib.sha256(blob).hexdigest()
    extension = _image_extension(media_path, blob)
    relative = Path("products") / digest[:2] / f"{digest}{extension}"
    absolute = Path(upload_root) / relative
    if not absolute.exists():
        absolute.parent.mkdir(parents=True, exist_ok=True)
        temporary = absolute.with_suffix(absolute.suffix + ".tmp")
        temporary.write_bytes(blob)
        temporary.replace(absolute)
    return "/media/" + relative.as_posix()


def _display_price(source_price_cents: int, multiplier: Decimal) -> int:
    result = (Decimal(source_price_cents) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(0, int(result))


def import_xlsx(
    connection: Any,
    path: str | Path,
    upload_root: str | Path,
    *,
    original_filename: str | None = None,
    price_multiplier: Decimal | str | float = Decimal("1"),
    deactivate_missing: bool = False,
) -> ImportResult:
    """Parse one XLSX and atomically upsert products by barcode."""
    _verify_archive(path)
    filename = original_filename or Path(path).name
    try:
        multiplier = Decimal(str(price_multiplier))
    except InvalidOperation as exc:
        raise ValueError("Некорректный коэффициент наценки") from exc
    if multiplier <= 0 or multiplier > Decimal("100"):
        raise ValueError("Коэффициент наценки должен быть больше 0 и не больше 100")

    products, media, warnings = parse_workbook(path)
    if not products:
        raise ValueError("В файле не найдено ни одного товара с моделью, штрихкодом и ценой")

    file_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    result = ImportResult(filename=filename, warnings=list(warnings))
    image_urls: dict[str, str] = {
        media_path: _save_image(blob, media_path, upload_root)
        for media_path, blob in media.items()
    }
    result.image_count = len(image_urls)

    brands_seen: dict[str, set[str]] = {}
    try:
        connection.execute("BEGIN IMMEDIATE")
        for product in products:
            brands_seen.setdefault(product.brand, set()).add(product.barcode)
            images = [image_urls[path] for path in product.media_paths if path in image_urls]
            images_json = json.dumps(images, ensure_ascii=False, separators=(",", ":"))
            price_cents = _display_price(product.price_cents, multiplier)
            existing = connection.execute(
                "SELECT * FROM products WHERE barcode = ?", (product.barcode,)
            ).fetchone()
            values = {
                "barcode": product.barcode,
                "brand": product.brand,
                "category": product.category,
                "model": product.model,
                "color": product.color,
                "box_qty": product.box_qty,
                "source_price_cents": product.price_cents,
                "price_cents": price_cents,
                "description": product.description,
                "badge": product.badge,
                "images_json": images_json,
                "source_file": filename,
                "source_sheet": product.sheet_name,
                "source_row": product.row_number,
            }
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO products(
                        barcode, brand, category, model, color, box_qty,
                        source_price_cents, price_cents, description, badge,
                        images_json, active, source_file, source_sheet, source_row
                    ) VALUES (
                        :barcode, :brand, :category, :model, :color, :box_qty,
                        :source_price_cents, :price_cents, :description, :badge,
                        :images_json, 1, :source_file, :source_sheet, :source_row
                    )
                    """,
                    values,
                )
                result.inserted += 1
                continue

            changed_fields = (
                "brand", "category", "model", "color", "box_qty",
                "source_price_cents", "price_cents", "description", "badge",
                "images_json", "source_file", "source_sheet", "source_row",
            )
            changed = any(existing[field] != values[field] for field in changed_fields) or existing["active"] != 1
            if changed:
                connection.execute(
                    """
                    UPDATE products SET
                        brand = :brand,
                        category = :category,
                        model = :model,
                        color = :color,
                        box_qty = :box_qty,
                        source_price_cents = :source_price_cents,
                        price_cents = :price_cents,
                        description = :description,
                        badge = :badge,
                        images_json = :images_json,
                        active = 1,
                        source_file = :source_file,
                        source_sheet = :source_sheet,
                        source_row = :source_row,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE barcode = :barcode
                    """,
                    values,
                )
                result.updated += 1
            else:
                result.unchanged += 1

        if deactivate_missing:
            for brand, seen in brands_seen.items():
                existing_rows = connection.execute(
                    "SELECT id, barcode FROM products WHERE brand = ? AND active = 1", (brand,)
                ).fetchall()
                missing_ids = [row["id"] for row in existing_rows if row["barcode"] not in seen]
                if missing_ids:
                    connection.executemany(
                        """
                        UPDATE products
                        SET active = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE id = ?
                        """,
                        [(product_id,) for product_id in missing_ids],
                    )
                    result.deactivated += len(missing_ids)

        result.skipped = len([warning for warning in warnings if "пропущена" in warning])
        connection.execute(
            """
            INSERT INTO import_runs(
                filename, file_hash, inserted, updated, unchanged,
                deactivated, skipped, image_count, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                filename,
                file_hash,
                result.inserted,
                result.updated,
                result.unchanged,
                result.deactivated,
                result.skipped,
                result.image_count,
                json.dumps(result.warnings or [], ensure_ascii=False),
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return result
