import os
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


class OfficeDocumentError(Exception):
    pass


_DOCX_EXTENSIONS = {".docx", ".docm"}
_PPTX_EXTENSIONS = {".pptx", ".pptm"}
_XLSX_EXTENSIONS = {".xlsx", ".xlsm"}
_ODF_EXTENSIONS = {".odt", ".odp", ".ods"}
_TEXT_EXTENSIONS = {".csv", ".tsv"}
_UNSUPPORTED_LEGACY_EXTENSIONS = {".doc", ".ppt", ".xls", ".rtf"}


def _natural_key(value: str) -> list[object]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_xml(archive: zipfile.ZipFile, member: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(archive.read(member))
    except (KeyError, ElementTree.ParseError) as exc:
        raise OfficeDocumentError(f"Could not read {member}") from exc


def _text_nodes(root: ElementTree.Element) -> list[str]:
    return [
        value
        for element in root.iter()
        if _local_name(element.tag) == "t"
        and (value := (element.text or "").strip())
    ]


def _attribute_value(element: ElementTree.Element, name: str) -> str | None:
    """Return an OOXML attribute without depending on its namespace prefix."""
    return next(
        (
            value
            for attribute, value in element.attrib.items()
            if _local_name(attribute) == name
        ),
        None,
    )


def _descendant_value(element: ElementTree.Element, name: str) -> str | None:
    child = next(
        (item for item in element.iter() if _local_name(item.tag) == name),
        None,
    )
    return _attribute_value(child, "val") if child is not None else None


def _to_letters(value: int, *, uppercase: bool) -> str:
    result = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(ord("A" if uppercase else "a") + remainder) + result
    return result


def _to_roman(value: int, *, uppercase: bool) -> str:
    numerals = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    result = ""
    for number, numeral in numerals:
        while value >= number:
            result += numeral
            value -= number
    return result if uppercase else result.lower()


def _format_list_counter(value: int, number_format: str) -> str:
    if number_format == "lowerLetter":
        return _to_letters(value, uppercase=False)
    if number_format == "upperLetter":
        return _to_letters(value, uppercase=True)
    if number_format == "lowerRoman":
        return _to_roman(value, uppercase=False)
    if number_format == "upperRoman":
        return _to_roman(value, uppercase=True)
    return str(value)


def _docx_numbering_definitions(
    archive: zipfile.ZipFile,
) -> dict[tuple[str, int], tuple[int, str, str]]:
    """Read list-instance and level definitions from ``numbering.xml``."""
    if "word/numbering.xml" not in archive.namelist():
        return {}

    root = _read_xml(archive, "word/numbering.xml")
    abstract_levels: dict[tuple[str, int], tuple[int, str, str]] = {}
    number_to_abstract: dict[str, str] = {}

    for element in root:
        local_name = _local_name(element.tag)
        if local_name == "abstractNum":
            abstract_id = _attribute_value(element, "abstractNumId")
            if abstract_id is None:
                continue
            for level in element:
                if _local_name(level.tag) != "lvl":
                    continue
                level_value = _attribute_value(level, "ilvl")
                if level_value is None or not level_value.isdigit():
                    continue
                start_value = _descendant_value(level, "start") or "1"
                number_format = _descendant_value(level, "numFmt") or "decimal"
                level_text = _descendant_value(level, "lvlText") or "%1."
                abstract_levels[(abstract_id, int(level_value))] = (
                    int(start_value) if start_value.isdigit() else 1,
                    number_format,
                    level_text,
                )
        elif local_name == "num":
            number_id = _attribute_value(element, "numId")
            abstract_id = _descendant_value(element, "abstractNumId")
            if number_id is not None and abstract_id is not None:
                number_to_abstract[number_id] = abstract_id

    levels = {
        (number_id, level): definition
        for number_id, abstract_id in number_to_abstract.items()
        for (candidate_id, level), definition in abstract_levels.items()
        if candidate_id == abstract_id
    }
    return levels


def _paragraph_numbering(paragraph: ElementTree.Element) -> tuple[str, int] | None:
    number_properties = next(
        (
            element
            for element in paragraph.iter()
            if _local_name(element.tag) == "numPr"
        ),
        None,
    )
    if number_properties is None:
        return None
    number_id = _descendant_value(number_properties, "numId")
    level = _descendant_value(number_properties, "ilvl") or "0"
    if number_id is None or not level.isdigit():
        return None
    return number_id, int(level)


def _numbered_paragraph_prefix(
    numbering: tuple[str, int],
    levels: dict[tuple[str, int], tuple[int, str, str]],
    counters: dict[tuple[str, int], int],
) -> str:
    number_id, level = numbering
    start, number_format, level_text = levels.get(
        (number_id, level), (1, "decimal", f"%{level + 1}.")
    )
    key = (number_id, level)
    counters[key] = counters.get(key, start - 1) + 1
    for candidate in list(counters):
        if candidate[0] == number_id and candidate[1] > level:
            del counters[candidate]

    prefix = level_text
    for candidate_level in range(9):
        candidate_key = (number_id, candidate_level)
        if candidate_key not in counters:
            continue
        candidate_format = levels.get(
            candidate_key, (1, "decimal", "")
        )[1]
        prefix = prefix.replace(
            f"%{candidate_level + 1}",
            _format_list_counter(counters[candidate_key], candidate_format),
        )
    if number_format == "bullet":
        return level_text
    return prefix


def _extract_docx(archive: zipfile.ZipFile) -> str:
    root = _read_xml(archive, "word/document.xml")
    numbering_levels = _docx_numbering_definitions(archive)
    numbering_counters: dict[tuple[str, int], int] = {}
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if _local_name(paragraph.tag) != "p":
            continue
        text = " ".join(_text_nodes(paragraph))
        numbering = _paragraph_numbering(paragraph)
        if numbering is not None:
            prefix = _numbered_paragraph_prefix(
                numbering, numbering_levels, numbering_counters
            )
            text = f"{prefix} {text}".strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_pptx(archive: zipfile.ZipFile) -> str:
    slide_members = sorted(
        (
            name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        ),
        key=_natural_key,
    )
    slides = [" ".join(_text_nodes(_read_xml(archive, member))) for member in slide_members]
    return "\n\n".join(slide for slide in slides if slide)


def _extract_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    return [
        " ".join(_text_nodes(item))
        for item in root.iter()
        if _local_name(item.tag) == "si"
    ]


def _extract_xlsx(archive: zipfile.ZipFile) -> str:
    shared_strings = _extract_shared_strings(archive)
    sheet_members = sorted(
        (
            name
            for name in archive.namelist()
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
        ),
        key=_natural_key,
    )
    sheets: list[str] = []
    for member in sheet_members:
        root = _read_xml(archive, member)
        rows: list[str] = []
        for row in root.iter():
            if _local_name(row.tag) != "row":
                continue
            values: list[str] = []
            for cell in row:
                if _local_name(cell.tag) != "c":
                    continue
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    value = " ".join(_text_nodes(cell))
                else:
                    raw_value = next(
                        (
                            (element.text or "").strip()
                            for element in cell
                            if _local_name(element.tag) == "v"
                        ),
                        "",
                    )
                    if cell_type == "s" and raw_value.isdigit():
                        index = int(raw_value)
                        value = (
                            shared_strings[index]
                            if index < len(shared_strings)
                            else raw_value
                        )
                    else:
                        value = raw_value
                if value:
                    values.append(value)
            if values:
                rows.append("\t".join(values))
        if rows:
            sheets.append("\n".join(rows))
    return "\n\n".join(sheets)


def _extract_odf(archive: zipfile.ZipFile) -> str:
    root = _read_xml(archive, "content.xml")
    values = [
        " ".join(text.strip() for text in element.itertext() if text.strip())
        for element in root.iter()
        if _local_name(element.tag) in {"p", "h"}
    ]
    return "\n".join(value for value in values if value)


def extract_office_document_text(file_path: str) -> str:
    extension = Path(file_path).suffix.lower()
    if extension in _TEXT_EXTENSIONS:
        with open(file_path, "r", encoding="utf-8", errors="replace") as file:
            return file.read()

    if extension in _UNSUPPORTED_LEGACY_EXTENSIONS:
        raise OfficeDocumentError(
            f"{extension} files require an external office conversion engine; "
            "save the document in a modern OOXML or OpenDocument format first"
        )

    try:
        with zipfile.ZipFile(file_path) as archive:
            if extension in _DOCX_EXTENSIONS:
                return _extract_docx(archive)
            if extension in _PPTX_EXTENSIONS:
                return _extract_pptx(archive)
            if extension in _XLSX_EXTENSIONS:
                return _extract_xlsx(archive)
            if extension in _ODF_EXTENSIONS:
                return _extract_odf(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise OfficeDocumentError(
            f"Could not parse {os.path.basename(file_path)}"
        ) from exc

    raise OfficeDocumentError(f"Unsupported office document format: {extension}")
