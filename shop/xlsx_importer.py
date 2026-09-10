from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import struct
import tempfile
import time
import warnings as image_warnings
import zipfile
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
# Element annotations and ParseError only; all XML is parsed by SafeET below.
import xml.etree.ElementTree as ET  # nosec B405

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from PIL import Image, UnidentifiedImageError

from .database import translate_database_busy

MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 64 * 1024 * 1024
MAX_XML_NODES = 750_000
MAX_ROWS = 50_000
MAX_CELLS = 500_000
MAX_CELL_TEXT = 32_768
MAX_SHARED_TEXT_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_TEXT_BYTES = 32 * 1024 * 1024
MAX_SHEET_NAME_BYTES = 512
MAX_BRAND_BYTES = 512
MAX_MODEL_BYTES = 1024
MAX_CATEGORY_BYTES = 512
MAX_COLOR_BYTES = 512
MAX_BOX_QTY_BYTES = 128
MAX_DESCRIPTION_BYTES = 64 * 1024
MAX_COMMENT_BYTES = 8 * 1024
MAX_BADGE_BYTES = 1024
MAX_IMAGES = 2_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 150 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_FRAMES = 50
MAX_IMAGE_FRAME_PIXELS = 32_000_000
MAX_TOTAL_IMAGE_PIXELS = 500_000_000
MAX_IMPORT_SECONDS = 60
MAX_PRICE_CENTS = 100_000_000_00


class ImportLimitError(ValueError):
    """An import exceeded its documented resource budget."""


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ImportLimitError("Превышен лимит времени обработки XLSX")


class WorkbookReader:
    """Bound actual decompression and XML work before building large trees."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.names = frozenset(archive.namelist())
        self.deadline = time.monotonic() + MAX_IMPORT_SECONDS
        self.xml_bytes = self.nodes = self.rows = self.cells = 0
        self.image_bytes = self.image_pixels = 0
        self.shared_text_bytes = self.expanded_text_bytes = 0
        self._xml_cache: OrderedDict[str, ET.Element] = OrderedDict()

    def check_time(self) -> None:
        _check_deadline(self.deadline)

    def add_expanded_text(self, value: str) -> None:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as exc:
            raise ValueError("Некорректный Unicode в XLSX") from exc
        self.expanded_text_bytes += size
        if self.expanded_text_bytes > MAX_EXPANDED_TEXT_BYTES:
            raise ImportLimitError("Превышен суммарный лимит развёрнутого текста XLSX")

    def namelist(self) -> frozenset[str]:
        return self.names

    def read(self, path: str, maximum: int = MAX_ENTRY_BYTES) -> bytes:
        self.check_time()
        if path not in self.names:
            raise ValueError("В XLSX отсутствует связанная часть")
        if self.archive.getinfo(path).file_size > maximum:
            raise ImportLimitError("Внутренний файл XLSX превышает безопасный лимит")
        with self.archive.open(path) as stream:
            blob = stream.read(maximum + 1)
        if len(blob) > maximum:
            raise ImportLimitError("Внутренний файл XLSX превышает безопасный лимит")
        return blob

    def xml(self, path: str) -> ET.Element:
        self.check_time()
        if path in self._xml_cache:
            self._xml_cache.move_to_end(path)
            return self._xml_cache[path]
        blob = self.read(path, MAX_XML_BYTES)
        self.xml_bytes += len(blob)
        if self.xml_bytes > MAX_TOTAL_XML_BYTES:
            raise ImportLimitError("Превышен суммарный лимит XML в XLSX")
        depth = 0
        try:
            parser = SafeET.iterparse(io.BytesIO(blob), events=("start", "end"), forbid_dtd=True)
            for event, node in parser:
                if event == "start":
                    depth += 1
                    self.nodes += 1
                    if depth > 64 or self.nodes > MAX_XML_NODES:
                        raise ImportLimitError("Превышен лимит сложности XML")
                    if self.nodes % 1024 == 0:
                        self.check_time()
                else:
                    depth -= 1
                    if len(node.text or "") > MAX_CELL_TEXT:
                        raise ImportLimitError("Текстовая ячейка XLSX слишком длинная")
                    if any(len(value) > MAX_CELL_TEXT for value in node.attrib.values()):
                        raise ImportLimitError("Атрибут XML слишком длинный")
            root = parser.root
        except (ET.ParseError, DefusedXmlException, LookupError) as exc:
            raise ValueError("Некорректный или неподдерживаемый XML в XLSX") from exc
        self._xml_cache[path] = root
        while len(self._xml_cache) > 2:
            self._xml_cache.popitem(last=False)
        return root

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
    if not target or any(char in target for char in ("\\", ":", "?", "#", "%", "\x00")):
        raise ValueError("Неподдерживаемая связь XLSX")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(part_path), target))
    if resolved.startswith("/"):
        resolved = resolved[1:]
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("Связь XLSX выходит за пределы книги")
    return resolved


def parse_relationships(zf: WorkbookReader, part_path: str) -> dict[str, str]:
    rp = rels_path(part_path)
    if rp not in zf.namelist():
        return {}
    root = zf.xml(rp)
    out: dict[str, str] = {}
    for rel in root.findall(q(NS_REL_PKG, "Relationship")):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rel.attrib.get("TargetMode", "Internal") != "Internal":
            raise ValueError("Внешние связи XLSX запрещены")
        if rid and target:
            if rid in out:
                raise ValueError("Повторяющаяся связь XLSX")
            out[rid] = resolve_target(part_path, target)
    return out


def get_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    text = "".join((t.text or "") for t in node.iter(q(NS_MAIN, "t")))
    if len(text) > MAX_CELL_TEXT:
        raise ImportLimitError("Текстовая ячейка XLSX слишком длинная")
    return text


def read_shared_strings(zf: WorkbookReader) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    root = zf.xml(path)
    strings = root.findall(q(NS_MAIN, "si"))
    if len(strings) > 100_000:
        raise ImportLimitError("Слишком много общих строк XLSX")
    result: list[str] = []
    for item in strings:
        value = get_text(item)
        zf.shared_text_bytes += len(value.encode("utf-8"))
        if zf.shared_text_bytes > MAX_SHARED_TEXT_BYTES:
            raise ImportLimitError("Превышен лимит хранимого текста общих строк XLSX")
        result.append(value)
    return result


def col_index(cell_ref: str) -> int:
    letters = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", cell_ref.upper())
    if not letters:
        raise ValueError("Некорректный адрес ячейки XLSX")
    value = 0
    for ch in letters.group(1):
        value = value * 26 + (ord(ch) - 64)
    if value > 16_384 or int(letters.group(2)) > 1_048_576:
        raise ValueError("Адрес ячейки вне допустимых пределов XLSX")
    return value - 1


def read_cell_value(zf: WorkbookReader, cell: ET.Element, shared: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        value = get_text(cell.find(q(NS_MAIN, "is")))
        zf.add_expanded_text(value)
        return value
    value_node = cell.find(q(NS_MAIN, "v"))
    raw = value_node.text if value_node is not None else None
    if raw is None:
        return None
    if cell_type == "s":
        try:
            index = int(raw)
            if index < 0:
                raise ValueError
            value = shared[index]
        except (ValueError, IndexError):
            raise ValueError("Некорректный индекс общей строки XLSX") from None
        # Charge each expansion, not only the shared table entry.
        zf.add_expanded_text(value)
        return value
    if cell_type == "b":
        zf.add_expanded_text(raw)
        return raw == "1"
    zf.add_expanded_text(raw)
    return raw


def read_sheet_rows(zf: WorkbookReader, sheet_path: str, shared: list[str]) -> dict[int, dict[int, Any]]:
    root = zf.xml(sheet_path)
    sheet_data = root.find(q(NS_MAIN, "sheetData"))
    rows: dict[int, dict[int, Any]] = {}
    if sheet_data is None:
        return rows
    for row in sheet_data.findall(q(NS_MAIN, "row")):
        zf.check_time()
        zf.rows += 1
        if zf.rows > MAX_ROWS:
            raise ImportLimitError("В XLSX слишком много строк")
        try:
            row_num = int(row.attrib.get("r", "0"))
        except ValueError as exc:
            raise ValueError("Некорректный номер строки XLSX") from exc
        if not 1 <= row_num <= 1_048_576 or row_num in rows:
            raise ValueError("Некорректный или повторяющийся номер строки XLSX")
        values: dict[int, Any] = {}
        for cell in row.findall(q(NS_MAIN, "c")):
            zf.cells += 1
            if zf.cells > MAX_CELLS:
                raise ImportLimitError("В XLSX слишком много ячеек")
            ref = cell.attrib.get("r", "A1")
            column = col_index(ref)
            if column in values:
                raise ValueError("Повторяющаяся ячейка XLSX")
            values[column] = read_cell_value(zf, cell, shared)
        rows[row_num] = values
    return rows


def drawing_for_sheet(zf: WorkbookReader, sheet_path: str) -> str | None:
    root = zf.xml(sheet_path)
    drawing = root.find(q(NS_MAIN, "drawing"))
    if drawing is None:
        return None
    rid = drawing.attrib.get(q(NS_REL_DOC, "id"))
    if not rid:
        return None
    return parse_relationships(zf, sheet_path).get(rid)


def read_image_anchors(zf: WorkbookReader, sheet_path: str) -> dict[int, list[tuple[int, int, str]]]:
    drawing_path = drawing_for_sheet(zf, sheet_path)
    if not drawing_path or drawing_path not in zf.namelist():
        return {}
    rels = parse_relationships(zf, drawing_path)
    root = zf.xml(drawing_path)
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
    if not best or not all(field in best[2] for field in ("model", "barcode", "price")):
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


def bounded_business_text(value: Any, maximum: int, field: str, *, title: bool = False) -> str:
    """Enforce UTF-8 field budgets before and after normalization."""
    raw = "" if value is None else str(value)
    try:
        if len(raw.encode("utf-8")) > maximum:
            raise ImportLimitError(f"Поле {field} превышает лимит текста XLSX")
    except UnicodeError as exc:
        raise ValueError("Некорректный Unicode в XLSX") from exc
    normalized = normalize_title_text(raw) if title else normalize_text(raw)
    if len(normalized.encode("utf-8")) > maximum:
        raise ImportLimitError(f"Поле {field} превышает лимит текста XLSX")
    return normalized


def normalize_barcode(value: Any) -> str:
    text = normalize_title_text(value)
    if not text or len(text) > 128 or isinstance(value, bool):
        return ""
    # Preserve leading zeroes for text barcodes. Convert only decimal/scientific
    # numeric representations produced by spreadsheet software.
    if not re.fullmatch(r"[0-9]+", text):
        if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", text):
            return ""
        try:
            decimal = Decimal(text.replace(",", "."))
            if decimal.is_finite() and 0 <= decimal < Decimal("1e18") and decimal == decimal.to_integral_value():
                text = format(decimal.quantize(Decimal("1")), "f")
            else:
                return ""
        except InvalidOperation:
            return ""
    digits = re.sub(r"\D", "", text)
    return digits if 6 <= len(digits) <= 18 else ""


def parse_price_cents(value: Any) -> int | None:
    text = normalize_title_text(value).replace(" ", "").replace("₽", "").replace("руб.", "").replace("руб", "")
    text = text.replace(",", ".")
    if not text or len(text) > 64 or isinstance(value, bool):
        return None
    match = re.fullmatch(r"[+]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]{1,3})?", text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(0))
        if not amount.is_finite() or amount <= 0 or amount > Decimal(MAX_PRICE_CENTS) / 100:
            return None
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return cents if 0 < cents <= MAX_PRICE_CENTS else None


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


@dataclass(frozen=True)
class ValidatedImage:
    blob: bytes
    extension: str
    digest: str


def parse_workbook(path: str | Path) -> tuple[list[ParsedProduct], dict[str, ValidatedImage], list[str]]:
    try:
        return _parse_workbook(path)
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, EOFError) as exc:
        raise ValueError("Файл повреждён или не является XLSX") from exc


def _parse_workbook(path: str | Path) -> tuple[list[ParsedProduct], dict[str, ValidatedImage], list[str]]:
    _verify_archive(path)
    products: list[ParsedProduct] = []
    media: dict[str, ValidatedImage] = {}
    warnings: list[str] = []
    with zipfile.ZipFile(path) as archive:
        zf = WorkbookReader(archive)
        _verify_package(zf)
        shared = read_shared_strings(zf)
        workbook_path = "xl/workbook.xml"
        root = zf.xml(workbook_path)
        workbook_rels = parse_relationships(zf, workbook_path)
        sheets_node = root.find(q(NS_MAIN, "sheets"))
        if sheets_node is None:
            return products, media, ["В книге нет листов"]
        sheets = sheets_node.findall(q(NS_MAIN, "sheet"))
        if len(sheets) > 100:
            raise ImportLimitError("В XLSX слишком много листов")
        seen_barcodes: set[str] = set()
        for sheet in sheets:
            zf.check_time()
            sheet_name = sheet.attrib.get("name", "Лист")
            zf.add_expanded_text(sheet_name)
            bounded_business_text(sheet_name, MAX_SHEET_NAME_BYTES, "название листа", title=True)
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
            brand = bounded_business_text(sheet_name, MAX_BRAND_BYTES, "бренд", title=True)
            for row_num in sorted(rows):
                zf.check_time()
                if row_num <= header_row:
                    continue
                row = rows[row_num]
                model = bounded_business_text(row.get(columns["model"]), MAX_MODEL_BYTES, "модель", title=True)
                barcode = normalize_barcode(row.get(columns["barcode"]))
                price_cents = parse_price_cents(row.get(columns["price"]))
                if not (model and barcode and price_cents):
                    # Empty formatting rows are allowed; malformed product rows
                    # reject the whole file before any catalog mutation.
                    if model or barcode or price_cents:
                        raise ValueError(f"Строка {row_num}: некорректные обязательные поля товара")
                    continue
                if barcode in seen_barcodes:
                    raise ValueError("В XLSX повторяется штрихкод товара")
                seen_barcodes.add(barcode)
                item_media = [m for _col, _off, m in images_by_row.get(row_num, [])]
                dedup_media: list[str] = []
                seen_hashes: set[str] = set()
                for media_path in item_media:
                    zf.check_time()
                    if media_path not in media:
                        if len(media) >= MAX_IMAGES:
                            raise ImportLimitError("В XLSX слишком много изображений")
                        blob = zf.read(media_path, MAX_IMAGE_BYTES)
                        zf.image_bytes += len(blob)
                        extension, decoded_pixels = _validate_image(blob, deadline=zf.deadline)
                        zf.image_pixels += decoded_pixels
                        if zf.image_bytes > MAX_TOTAL_IMAGE_BYTES or zf.image_pixels > MAX_TOTAL_IMAGE_PIXELS:
                            raise ImportLimitError("Превышен суммарный лимит изображений XLSX")
                        media[media_path] = ValidatedImage(
                            blob=blob, extension=extension, digest=hashlib.sha256(blob).hexdigest()
                        )
                    image = media[media_path]
                    digest = image.digest
                    if digest in seen_hashes:
                        continue
                    seen_hashes.add(digest)
                    dedup_media.append(media_path)
                comment = (bounded_business_text(row.get(columns.get("comment", -1)), MAX_COMMENT_BYTES,
                                                 "комментарий") if "comment" in columns else "")
                badge = "Новинка" if "новин" in comment.lower() else comment
                badge = bounded_business_text(badge, MAX_BADGE_BYTES, "метка")
                products.append(ParsedProduct(
                    brand=brand,
                    category=bounded_business_text(row.get(columns.get("category", -1)), MAX_CATEGORY_BYTES,
                                                   "категория", title=True),
                    model=model,
                    color=bounded_business_text(row.get(columns.get("color", -1)), MAX_COLOR_BYTES,
                                                "цвет", title=True),
                    barcode=barcode,
                    box_qty=bounded_business_text(row.get(columns.get("box_qty", -1)), MAX_BOX_QTY_BYTES,
                                                  "количество в коробке", title=True),
                    price_cents=price_cents,
                    description=bounded_business_text(row.get(columns.get("description", -1)),
                                                      MAX_DESCRIPTION_BYTES, "описание"),
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
        if Path(path).stat().st_size > MAX_FILE_BYTES:
            raise ImportLimitError("XLSX превышает лимит размера файла")
        with zipfile.ZipFile(path) as zf:
            if len(zf.infolist()) > 10_000:
                raise ImportLimitError("В XLSX слишком много внутренних файлов")
            total_size = sum(info.file_size for info in zf.infolist())
            if total_size > max_uncompressed_bytes:
                raise ImportLimitError("Распакованный XLSX превышает безопасный лимит")
            xml_size = sum(info.file_size for info in zf.infolist() if info.filename.endswith((".xml", ".rels")))
            if xml_size > MAX_TOTAL_XML_BYTES:
                raise ImportLimitError("Превышен суммарный лимит XML в XLSX")
            names: set[str] = set()
            for info in zf.infolist():
                name = info.filename
                if name in names:
                    raise ValueError("В XLSX повторяются имена внутренних файлов")
                names.add(name)
                if (not name or name.startswith("/") or any(char in name for char in ("\\", ":", "\x00"))
                        or ".." in name.split("/") or len(name) > 255):
                    raise ValueError("Некорректный путь внутри XLSX")
                if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError("Неподдерживаемое шифрование или сжатие XLSX")
                if info.file_size > MAX_ENTRY_BYTES:
                    raise ImportLimitError("Внутренний файл XLSX превышает безопасный лимит")
                if info.is_dir():
                    continue
                suffix = Path(name).suffix.lower()
                if suffix not in {".xml", ".rels", ".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    raise ValueError("Неподдерживаемое содержимое XLSX")
                if any(part.lower() in {"externallinks", "embeddings", "activex", "macros"} for part in name.split("/")):
                    raise ValueError("Активное или внешнее содержимое XLSX запрещено")
                if suffix in {".xml", ".rels"} and info.file_size > MAX_XML_BYTES:
                    raise ImportLimitError("XML в XLSX превышает безопасный лимит")
            if "xl/workbook.xml" not in zf.namelist():
                raise ValueError("Файл не похож на корректную книгу XLSX")
    except (zipfile.BadZipFile, NotImplementedError, EOFError) as exc:
        raise ValueError("Файл повреждён или не является XLSX") from exc


def _verify_package(zf: WorkbookReader) -> None:
    allowed_types = {
        "application/xml", "application/vnd.openxmlformats-package.relationships+xml",
        "application/vnd.openxmlformats-package.core-properties+xml",
        "application/vnd.openxmlformats-officedocument.extended-properties+xml",
        "application/vnd.openxmlformats-officedocument.custom-properties+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml",
        "application/vnd.openxmlformats-officedocument.drawing+xml",
        "application/vnd.openxmlformats-officedocument.theme+xml",
        "image/png", "image/jpeg", "image/gif", "image/webp",
    }
    if "[Content_Types].xml" not in zf.names:
        raise ValueError("В XLSX отсутствует описание типов содержимого")
    for item in zf.xml("[Content_Types].xml"):
        if item.attrib.get("ContentType") not in allowed_types:
            raise ValueError("Неподдерживаемый тип содержимого XLSX")
    # Check every relationships part, including parts the catalog does not use.
    for name in sorted(zf.names):
        if not name.endswith(".rels"):
            continue
        root = zf.xml(name)
        for rel in root:
            if rel.attrib.get("TargetMode", "Internal") != "Internal":
                raise ValueError("Внешние связи XLSX запрещены")
            target = rel.attrib.get("Target", "")
            directory, filename = posixpath.split(name)
            part = posixpath.join(posixpath.dirname(directory), filename[:-5])
            resolved = resolve_target(part, target)
            if resolved not in zf.names:
                raise ValueError("В XLSX отсутствует связанная часть")


def _validate_image(blob: bytes, *, deadline: float | None = None) -> tuple[str, int]:
    if len(blob) > MAX_IMAGE_BYTES:
        raise ImportLimitError("Изображение превышает лимит размера")
    formats = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}
    try:
        with image_warnings.catch_warnings():
            image_warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(blob), formats=list(formats)) as img:
                extension = formats.get(img.format or "")
                if not extension:
                    raise ValueError("Неподдерживаемый формат изображения")
                width, height = img.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise ImportLimitError("Изображение превышает лимит пикселей")
                img.verify()
            # Decode every allowed frame: header signatures alone do not validate images.
            with Image.open(io.BytesIO(blob), formats=list(formats)) as img:
                pixels = 0
                for index in range(MAX_IMAGE_FRAMES + 1):
                    if deadline is not None:
                        _check_deadline(deadline)
                    try:
                        img.seek(index)
                    except EOFError:
                        break
                    if index >= MAX_IMAGE_FRAMES:
                        raise ImportLimitError("Изображение содержит слишком много кадров")
                    width, height = img.size
                    pixels += width * height
                    if width * height > MAX_IMAGE_PIXELS or pixels > MAX_IMAGE_FRAME_PIXELS:
                        raise ImportLimitError("Кадры изображения превышают лимит пикселей")
                    img.load()
                return extension, pixels
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ImportLimitError("Изображение превышает безопасный лимит пикселей") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, struct.error) as exc:
        raise ValueError("Некорректное изображение XLSX") from exc


def _image_extension(media_path: str, blob: bytes) -> str:
    return _validate_image(blob)[0]


@dataclass
class PreparedImage:
    url: str
    absolute: Path
    temporary: Path | None

    def publish(self, created: set[Path]) -> None:
        if self.temporary is None:
            return
        if self.absolute.exists():
            self.temporary.unlink(missing_ok=True)
        else:
            self.temporary.replace(self.absolute)
            created.add(self.absolute)
        self.temporary = None

    def cleanup(self) -> None:
        if self.temporary is not None:
            self.temporary.unlink(missing_ok=True)
            self.temporary = None


def _prepare_image(image: ValidatedImage, upload_root: str | Path) -> PreparedImage:
    """Write validated bytes before the SQLite writer transaction starts."""
    relative = Path("products") / image.digest[:2] / f"{image.digest}{image.extension}"
    absolute = Path(upload_root) / relative
    if not absolute.resolve().is_relative_to(Path(upload_root).resolve()):
        raise ValueError("Недопустимый путь изображения")
    temporary: Path | None = None
    if not absolute.exists():
        absolute.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=absolute.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(image.blob)
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
    return PreparedImage("/media/" + relative.as_posix(), absolute, temporary)


def _prepare_images(
    media: dict[str, ValidatedImage], upload_root: str | Path,
) -> tuple[dict[str, PreparedImage], list[PreparedImage]]:
    by_identity: dict[tuple[str, str], PreparedImage] = {}
    by_media_path: dict[str, PreparedImage] = {}
    try:
        for media_path, image in media.items():
            identity = (image.digest, image.extension)
            prepared = by_identity.get(identity)
            if prepared is None:
                prepared = _prepare_image(image, upload_root)
                by_identity[identity] = prepared
            by_media_path[media_path] = prepared
        return by_media_path, list(by_identity.values())
    except BaseException:
        for prepared in by_identity.values():
            prepared.cleanup()
        raise


def _display_price(source_price_cents: int, multiplier: Decimal) -> int:
    result = (Decimal(source_price_cents) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if not result.is_finite() or result < 1 or result > MAX_PRICE_CENTS:
        raise ValueError("Цена с наценкой выходит за допустимые пределы")
    return int(result)


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
    deadline = time.monotonic() + MAX_IMPORT_SECONDS
    filename = original_filename or Path(path).name
    try:
        multiplier = Decimal(str(price_multiplier))
    except InvalidOperation as exc:
        raise ValueError("Некорректный коэффициент наценки") from exc
    if not multiplier.is_finite() or multiplier <= 0 or multiplier > Decimal("100"):
        raise ValueError("Коэффициент наценки должен быть больше 0 и не больше 100")

    products, media, warnings = parse_workbook(path)
    _check_deadline(deadline)
    if not products:
        raise ValueError("В файле не найдено ни одного товара с моделью, штрихкодом и ценой")

    with Path(path).open("rb") as stream:
        file_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    result = ImportResult(filename=filename, warnings=list(warnings))
    created: set[Path] = set()
    brands_seen: dict[str, set[str]] = {}
    prepared_by_path, prepared_images = _prepare_images(media, upload_root)
    transaction_started = False
    savepoint_started = False
    try:
        _check_deadline(deadline)
        # Parsing, image decoding and temporary file writes are complete. The
        # writer lock now covers only quick publication and catalog mutations.
        with translate_database_busy():
            connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        connection.execute("SAVEPOINT catalog_import")
        savepoint_started = True
        for prepared in prepared_images:
            prepared.publish(created)
        image_urls = {media_path: prepared.url for media_path, prepared in prepared_by_path.items()}
        result.image_count = len(image_urls)
        for product in products:
            _check_deadline(deadline)
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
    except BaseException:
        if transaction_started:
            try:
                if savepoint_started:
                    connection.execute("ROLLBACK TO SAVEPOINT catalog_import")
                    root = Path(upload_root).resolve()
                    for image_path in created:
                        if not image_path.resolve().is_relative_to(root):
                            continue
                        url = "/media/" + image_path.resolve().relative_to(root).as_posix()
                        in_use = connection.execute(
                            "SELECT 1 FROM products WHERE instr(images_json, ?) > 0 LIMIT 1", (url,),
                        ).fetchone()
                        if not in_use:
                            image_path.unlink(missing_ok=True)
            finally:
                connection.rollback()
        raise
    finally:
        for prepared in prepared_images:
            prepared.cleanup()
    return result
