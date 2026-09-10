from __future__ import annotations

import hashlib
import io
import json
import struct
import subprocess
import sys
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from PIL import Image

from shop.database import connect, init_db
from shop import import_service
from shop import xlsx_importer as importer


def image_bytes(image_format: str = "PNG", color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(output, format=image_format)
    return output.getvalue()


def workbook(path: Path, *, price: str = "12.50", blob: bytes | None = None,
             additional: dict[str, str | bytes] | None = None,
             extra_rows: str = "", barcode: str = "0012345678901") -> Path:
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '</Types>',
        "xl/workbook.xml": f'<workbook xmlns="{importer.NS_MAIN}" xmlns:r="{importer.NS_REL_DOC}">'
        '<sheets><sheet name="Synthetic" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{importer.NS_REL_PKG}">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{importer.NS_MAIN}" xmlns:r="{importer.NS_REL_DOC}">'
        '<sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Модель</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Штрихкод</t></is></c>'
        '<c r="C1" t="inlineStr"><is><t>Цена</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Synthetic &lt;model&gt;</t></is></c>'
        f'<c r="B2" t="inlineStr"><is><t>{escape(barcode)}</t></is></c>'
        f'<c r="C2"><v>{escape(price)}</v></c></row>{extra_rows}</sheetData>'
        + ('<drawing r:id="image"/>' if blob is not None else '') + '</worksheet>',
    }
    if blob is not None:
        parts.update({
            "xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{importer.NS_REL_PKG}">'
            '<Relationship Id="image" Target="../drawings/drawing1.xml" Type="drawing"/></Relationships>',
            "xl/drawings/drawing1.xml": f'<xdr:wsDr xmlns:xdr="{importer.NS_XDR}" '
            f'xmlns:a="{importer.NS_A}" xmlns:r="{importer.NS_REL_DOC}">'
            '<xdr:oneCellAnchor><xdr:from><xdr:col>3</xdr:col><xdr:row>1</xdr:row></xdr:from>'
            '<xdr:pic><xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic>'
            '</xdr:oneCellAnchor></xdr:wsDr>',
            "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships xmlns="{importer.NS_REL_PKG}">'
            '<Relationship Id="rId1" Target="../media/image1.png" Type="image"/></Relationships>',
            "xl/media/image1.png": blob,
        })
    parts.update(additional or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return path


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.sqlite"
    init_db(path)
    connection = connect(path)
    try:
        yield connection, path, tmp_path / "media"
    finally:
        connection.close()


@pytest.mark.parametrize("image_format,extension", [("PNG", ".png"), ("JPEG", ".jpg"), ("GIF", ".gif"), ("WEBP", ".webp")])
def test_valid_import_images_and_barcode_upsert(tmp_path, catalog, image_format, extension):
    connection, _database, media = catalog
    blob = image_bytes(image_format)
    path = workbook(tmp_path / "price.xlsx", blob=blob)
    first = importer.import_xlsx(connection, path, media)
    row = connection.execute("SELECT * FROM products").fetchone()
    original_id = row["id"]
    assert first.inserted == 1 and first.image_count == 1
    assert row["barcode"] == "0012345678901" and row["price_cents"] == 1250
    url = json.loads(row["images_json"])[0]
    digest = hashlib.sha256(blob).hexdigest()
    assert url.endswith(digest + extension)
    assert (media / url.removeprefix("/media/")).read_bytes() == blob
    assert importer.import_xlsx(connection, path, media).unchanged == 1
    workbook(path, price="15.00", blob=blob)
    assert importer.import_xlsx(connection, path, media).updated == 1
    updated = connection.execute("SELECT * FROM products").fetchall()
    assert len(updated) == 1 and updated[0]["id"] == original_id
    assert updated[0]["price_cents"] == 1500


@pytest.mark.parametrize("blob", [b"<html>not an image</html>", b"\x89PNG\r\n\x1a\n", b"GIF89a", b"RIFF0000WEBP"])
def test_fake_or_truncated_images_reject_without_files(tmp_path, catalog, blob):
    connection, _database, media = catalog
    with pytest.raises(ValueError):
        importer.import_xlsx(connection, workbook(tmp_path / "bad.xlsx", blob=blob), media)
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0
    assert not media.exists()


def test_image_pixel_and_animation_budgets(monkeypatch):
    monkeypatch.setattr(importer, "MAX_IMAGE_PIXELS", 10)
    with pytest.raises(importer.ImportLimitError):
        importer._image_extension("fake.jpg", image_bytes())
    monkeypatch.setattr(importer, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(importer, "MAX_IMAGE_FRAMES", 2)
    output = io.BytesIO()
    frames = [Image.new("RGB", (4, 4), color) for color in ("red", "green", "blue")]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(importer.ImportLimitError):
        importer._image_extension("image.gif", output.getvalue())


@pytest.mark.parametrize("additional", [
    {"xl/_rels/unused.xml.rels": '<Relationships><Relationship Id="x" Target="https://example.invalid/image.png" TargetMode="External"/></Relationships>'},
    {"xl/vbaProject.bin": b"synthetic macro placeholder"},
    {"../outside.xml": "<part/>"},
    {"xl/media/unsupported.svg": "<svg/>"},
    {"xl/workbook.xml": '<!DOCTYPE x [<!ENTITY y "synthetic">]><x>&y;</x>'},
])
def test_external_active_and_unsupported_package_content_rejected(tmp_path, catalog, additional):
    connection, _database, media = catalog
    with pytest.raises(ValueError):
        importer.import_xlsx(connection, workbook(tmp_path / "bad.xlsx", additional=additional), media)
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0


@pytest.mark.parametrize("limit,value", [
    ("MAX_ENTRY_BYTES", 100), ("MAX_XML_BYTES", 100), ("MAX_TOTAL_XML_BYTES", 100),
    ("MAX_XML_NODES", 5), ("MAX_ROWS", 1), ("MAX_CELLS", 2), ("MAX_CELL_TEXT", 4),
    ("MAX_IMPORT_SECONDS", -1), ("MAX_IMAGES", 0), ("MAX_IMAGE_BYTES", 10),
    ("MAX_TOTAL_IMAGE_BYTES", 10), ("MAX_TOTAL_IMAGE_PIXELS", 10),
])
def test_resource_budgets_are_enforced(tmp_path, catalog, monkeypatch, limit, value):
    connection, _database, media = catalog
    path = workbook(tmp_path / "large.xlsx", blob=image_bytes())
    monkeypatch.setattr(importer, limit, value)
    with pytest.raises(importer.ImportLimitError):
        importer.import_xlsx(connection, path, media)
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_existing_zip_entry_count_and_total_inflated_limits(tmp_path):
    path = tmp_path / "entries.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
        for number in range(10_000):
            archive.writestr(f"part{number}.xml", "")
    with pytest.raises(importer.ImportLimitError):
        importer._verify_archive(path)
    workbook(path)
    with pytest.raises(importer.ImportLimitError):
        importer._verify_archive(path, max_uncompressed_bytes=100)
    # Tiny physical fixture advertises >300 MiB via ZIP directory metadata;
    # verify rejection before any decompression, without creating a large bomb.
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
        for number in range(10):
            archive.writestr(f"part{number}.xml", "<part/>")
    contents = bytearray(path.read_bytes())
    offset = 0
    while (offset := contents.find(b"PK\x01\x02", offset)) != -1:
        struct.pack_into("<I", contents, offset + 24, 31 * 1024 * 1024)
        offset += 4
    path.write_bytes(contents)
    with pytest.raises(importer.ImportLimitError, match="Распакованный"):
        importer._verify_archive(path)


@pytest.mark.parametrize("price", ["NaN", "Infinity", "-Infinity", "1e999", "9" * 100, "12garbage"])
def test_malformed_prices_fail_atomically(tmp_path, catalog, price):
    connection, _database, media = catalog
    with pytest.raises(ValueError):
        importer.import_xlsx(connection, workbook(tmp_path / "bad.xlsx", price=price), media)
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0


@pytest.mark.parametrize("multiplier", ["NaN", "Infinity", "-Infinity", "0", "101"])
def test_invalid_multiplier_is_controlled(tmp_path, catalog, multiplier):
    connection, _database, media = catalog
    with pytest.raises(ValueError):
        importer.import_xlsx(connection, workbook(tmp_path / "price.xlsx"), media, price_multiplier=multiplier)


def test_database_failure_rolls_back_catalog_and_only_new_images(tmp_path, catalog):
    connection, _database, media = catalog
    original_blob = image_bytes(color="red")
    path = workbook(tmp_path / "price.xlsx", blob=original_blob)
    importer.import_xlsx(connection, path, media)
    existing_images = set(media.rglob("*.png"))
    # Preserve a second product referencing the existing image through failed updates.
    connection.execute("INSERT INTO products(barcode,brand,model,source_price_cents,price_cents,images_json) "
                       "SELECT '0012345678902',brand,'Other synthetic',1,1,images_json FROM products LIMIT 1")
    connection.execute("CREATE TRIGGER reject_import_run BEFORE INSERT ON import_runs BEGIN "
                       "SELECT RAISE(ABORT, 'synthetic database failure'); END")
    connection.commit()
    workbook(path, blob=image_bytes(color="blue"), price="50")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        importer.import_xlsx(connection, path, media)
    assert set(media.rglob("*.png")) == existing_images
    assert not list(media.rglob("*.tmp"))
    assert connection.execute("SELECT price_cents FROM products WHERE barcode='0012345678901'").fetchone()[0] == 1250
    assert connection.execute("SELECT count(*) FROM import_runs").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 2


def test_duplicate_barcode_rejects_file(tmp_path, catalog):
    connection, _database, media = catalog
    duplicate = '<row r="3"><c r="A3" t="inlineStr"><is><t>Duplicate</t></is></c>'
    duplicate += '<c r="B3"><v>0012345678901</v></c><c r="C3"><v>10</v></c></row>'
    with pytest.raises(ValueError, match="штрихкод"):
        importer.import_xlsx(connection, workbook(tmp_path / "duplicate.xlsx", extra_rows=duplicate), media)


def test_scientific_numeric_cells_and_invalid_barcode_text(tmp_path, catalog):
    connection, _database, media = catalog
    path = workbook(tmp_path / "numeric.xlsx", price="1.25e2", barcode="1.23456789e8")
    importer.import_xlsx(connection, path, media)
    row = connection.execute("SELECT barcode,price_cents FROM products").fetchone()
    assert row["barcode"] == "123456789" and row["price_cents"] == 12500
    assert importer.normalize_barcode("bad12345678") == ""
    assert importer.normalize_barcode("123456.5") == ""


def test_corrupt_deflate_and_unknown_xml_encoding_are_controlled(tmp_path, catalog):
    connection, _database, media = catalog
    path = workbook(tmp_path / "encoding.xlsx", additional={
        "xl/workbook.xml": '<?xml version="1.0" encoding="synthetic-unknown"?><workbook/>'
    })
    with pytest.raises(ValueError):
        importer.import_xlsx(connection, path, media)
    path = workbook(tmp_path / "corrupt.xlsx")
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("xl/workbook.xml")
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    corrupted = bytearray(path.read_bytes())
    corrupted[offset:offset + info.compress_size] = b"\xff" * info.compress_size
    path.write_bytes(corrupted)
    with pytest.raises(ValueError):
        importer.import_xlsx(connection, path, media)
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_deactivation_targets_only_seen_brands(tmp_path, catalog):
    connection, _database, media = catalog
    path = workbook(tmp_path / "prices.xlsx")
    importer.import_xlsx(connection, path, media)
    connection.execute("INSERT INTO products(barcode,brand,model,source_price_cents,price_cents) "
                       "VALUES ('0012345678902','Synthetic','Missing synthetic',1,1),"
                       "('0012345678903','Other brand','Other synthetic',1,1)")
    connection.commit()
    result = importer.import_xlsx(connection, path, media, deactivate_missing=True)
    assert result.deactivated == 1
    rows = {row["barcode"]: row["active"] for row in connection.execute("SELECT barcode,active FROM products")}
    assert rows == {"0012345678901": 1, "0012345678902": 0, "0012345678903": 1}


def test_batch_upload_limits_precede_catalog_mutation_and_clean_staging(tmp_path, catalog):
    connection, database, media = catalog
    blob = workbook(tmp_path / "price.xlsx").read_bytes()
    with pytest.raises(importer.ImportLimitError):
        import_service.run_imports(database, media, tmp_path / "imports",
                                   [("a.xlsx", io.BytesIO(blob)), ("b.xlsx", io.BytesIO(blob))],
                                   max_total_bytes=len(blob) + 1)
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 0
    assert not list((tmp_path / "imports").rglob("*.xlsx"))


def test_ten_files_and_archive_retention_keep_legacy_files(tmp_path, catalog, monkeypatch):
    _connection, database, media = catalog
    imports = tmp_path / "imports"
    imports.mkdir()
    legacy = imports / "legacy.xlsx"
    legacy.write_bytes(b"synthetic legacy archive")
    monkeypatch.setattr(import_service, "MAX_ARCHIVE_FILES", 3)
    files = []
    for number in range(10):
        path = workbook(tmp_path / "price.xlsx", barcode=f"00123456789{number:02d}")
        files.append((f"price{number}.xlsx", io.BytesIO(path.read_bytes())))
    completed, failed = import_service.run_imports(database, media, imports, files)
    assert len(completed) == 10 and failed == []
    assert len(list((imports / "managed-v1").glob("xlsx-*.xlsx"))) == 3
    assert legacy.read_bytes() == b"synthetic legacy archive"
    assert not list((imports / "managed-v1").glob("stage-*"))


def test_failed_service_import_removes_staging_and_preserves_old_catalog(tmp_path, catalog):
    connection, database, media = catalog
    source = workbook(tmp_path / "price.xlsx")
    importer.import_xlsx(connection, source, media)
    imports = tmp_path / "imports"
    managed = imports / "managed-v1"
    managed.mkdir(parents=True)
    stale = managed / ("stage-" + "0" * 32 + ".xlsx")
    stale.write_bytes(b"synthetic abandoned stage")
    completed, failed = import_service.run_imports(
        database, media, imports, [("bad.xlsx", io.BytesIO(b"not a zip"))],
    )
    assert completed == [] and len(failed) == 1
    assert not list(managed.glob("*.xlsx"))
    assert connection.execute("SELECT count(*) FROM products").fetchone()[0] == 1


def test_import_lock_cross_process_and_release(tmp_path):
    media = tmp_path / "media"
    code = (
        "import sys\nfrom shop.import_service import import_slot, ImportBusyError\n"
        "try:\n with import_slot(sys.argv[1]): pass\n"
        "except ImportBusyError: sys.exit(23)\n"
    )
    with import_service.import_slot(media):
        result = subprocess.run([sys.executable, "-c", code, str(media)], capture_output=True, timeout=30)
        assert result.returncode == 23, result.stderr.decode(errors="replace")
    abrupt_exit = (
        "import os,sys\nfrom shop.import_service import import_slot\n"
        "with import_slot(sys.argv[1]): os._exit(0)\n"
    )
    result = subprocess.run([sys.executable, "-c", abrupt_exit, str(media)], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    with import_service.import_slot(media):
        pass
