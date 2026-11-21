"""Manifest loading and validation.

Manifest format (JSON):

    {
      "tables": [
        {"name": "S",
         "path": "S.jsonl",                 # relative to the manifest file
         "columns": [{"name": "x", "type": "text"},
                     {"name": "y", "type": "int64"}]}
      ]
    }

Table names and column names have independent identity: they are compared
byte-for-byte, never trimmed, never case-folded.  A column named "row_id" is
reserved and rejected.
"""

import json
import os

from .errors import DataError, UsageError

COLUMN_TYPES = ("text", "int64")
ROW_ID_FIELD = "row_id"


class Column:
    __slots__ = ("name", "type")

    def __init__(self, name, type_):
        self.name = name
        self.type = type_


class Table:
    __slots__ = ("name", "path", "columns")

    def __init__(self, name, path, columns):
        self.name = name
        self.path = path
        self.columns = columns


class Manifest:
    __slots__ = ("tables", "base_dir")

    def __init__(self, tables, base_dir):
        self.tables = tables
        self.base_dir = base_dir


def load_manifest(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise UsageError("cannot read manifest %r: %s" % (path, exc))
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError("manifest %r is not valid JSON: %s" % (path, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("tables"), list):
        raise DataError("manifest must be a JSON object with a 'tables' array")

    base_dir = os.path.dirname(os.path.abspath(path))
    tables = []
    seen_tables = set()
    for tpos, tdoc in enumerate(doc["tables"]):
        if not isinstance(tdoc, dict):
            raise DataError("manifest tables[%d] is not an object" % tpos)
        name = tdoc.get("name")
        if not isinstance(name, str) or not name:
            raise DataError("manifest tables[%d] has no valid 'name'" % tpos)
        if name in seen_tables:
            raise DataError("duplicate table name %r in manifest" % name)
        seen_tables.add(name)
        tpath = tdoc.get("path")
        if not isinstance(tpath, str) or not tpath:
            raise DataError("table %r has no valid 'path'" % name)
        cols_doc = tdoc.get("columns")
        if not isinstance(cols_doc, list) or not cols_doc:
            raise DataError("table %r must declare a non-empty 'columns' array" % name)
        columns = []
        seen_cols = set()
        for cpos, cdoc in enumerate(cols_doc):
            if not isinstance(cdoc, dict):
                raise DataError("table %r columns[%d] is not an object" % (name, cpos))
            cname = cdoc.get("name")
            ctype = cdoc.get("type")
            if not isinstance(cname, str) or not cname:
                raise DataError("table %r columns[%d] has no valid 'name'" % (name, cpos))
            if cname == ROW_ID_FIELD:
                raise DataError(
                    "table %r: column name %r is reserved" % (name, ROW_ID_FIELD))
            if cname in seen_cols:
                raise DataError("duplicate column name %r in table %r" % (cname, name))
            seen_cols.add(cname)
            if ctype not in COLUMN_TYPES:
                raise DataError(
                    "table %r column %r: unknown type %r (expected one of %s)"
                    % (name, cname, ctype, ", ".join(COLUMN_TYPES)))
            columns.append(Column(cname, ctype))
        tables.append(Table(name, os.path.join(base_dir, tpath), columns))
    if not tables:
        raise DataError("manifest declares no tables")
    return Manifest(tables, base_dir)
