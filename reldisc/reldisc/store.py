"""Prepared on-disk store.

Layout of a workdir produced by ``prepare``::

    workdir/
      store.json                 # meta: format, domain sizes, tables, columns
      dict_text.json             # global text dictionary:  code -> value
      dict_int64.json            # global int64 dictionary: code -> value
      tables/t000/rowids.json    # row_ids in row order
      tables/t000/c000.bin       # int64 codes for column 0 (array('q') raw)
      tables/t000/c001.bin
      ...

Every value of every column is stored as a typed integer *code* into a global
per-type dictionary (one dictionary for text, one for int64, so the text value
"1" and the integer 1 can never collide).  The code ``domain_size(type)`` is
reserved as the null sentinel; the per-position radix used by the discovery
tuple keys is ``domain_size(type) + 1``.  Codes are an exact bijection with
typed values, so comparing tuples of codes is exact -- no hashing, no
collisions.

Loaded columns, row ids and dictionaries are cached in memory on first use;
a process restarted with only the workdir can run check/witnesses/discover.
"""

import json
import os
from array import array

from .errors import DataError, UsageError

STORE_FORMAT = 1
CODE_ARRAY_TYPE = "q"  # signed 64-bit


class Store:
    def __init__(self, workdir):
        self.workdir = workdir
        self._meta = None
        self._dicts = {}
        self._columns = {}
        self._rowids = {}

    # ----- paths ---------------------------------------------------------
    def meta_path(self):
        return os.path.join(self.workdir, "store.json")

    def table_dir(self, tidx):
        return os.path.join(self.workdir, "tables", "t%03d" % tidx)

    def column_path(self, tidx, cidx):
        return os.path.join(self.table_dir(tidx), "c%03d.bin" % cidx)

    def rowids_path(self, tidx):
        return os.path.join(self.table_dir(tidx), "rowids.json")

    def dict_path(self, typename):
        return os.path.join(self.workdir, "dict_%s.json" % typename)

    def exists(self):
        return os.path.exists(self.meta_path())

    # ----- meta ----------------------------------------------------------
    def write_meta(self, meta):
        tmp = self.meta_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.meta_path())
        self._meta = meta

    def meta(self):
        if self._meta is None:
            if not self.exists():
                raise UsageError(
                    "workdir %r is not prepared; run 'prepare' first" % self.workdir)
            try:
                with open(self.meta_path(), "r", encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise DataError("prepared store is corrupt: %s" % exc)
            if not isinstance(meta, dict) or meta.get("format") != STORE_FORMAT:
                raise DataError("prepared store has an unsupported format")
            self._meta = meta
        return self._meta

    # ----- typed domains -------------------------------------------------
    def domain_size(self, typename):
        return self.meta()["domain_sizes"][typename]

    def null_code(self, typename):
        """Code reserved for null inside columns of this type."""
        return self.domain_size(typename)

    def radix(self, typename):
        """Per-position radix for tuple keys of this type (domain + null)."""
        return self.domain_size(typename) + 1

    # ----- columns -------------------------------------------------------
    def save_column(self, tidx, cidx, codes):
        os.makedirs(self.table_dir(tidx), exist_ok=True)
        if codes.typecode != CODE_ARRAY_TYPE:
            codes = array(CODE_ARRAY_TYPE, codes)
        with open(self.column_path(tidx, cidx), "wb") as fh:
            codes.tofile(fh)

    def load_column(self, tidx, cidx):
        key = (tidx, cidx)
        if key not in self._columns:
            path = self.column_path(tidx, cidx)
            codes = array(CODE_ARRAY_TYPE)
            try:
                with open(path, "rb") as fh:
                    codes.fromfile(fh, os.path.getsize(path) // codes.itemsize)
            except OSError as exc:
                raise DataError("prepared column file %r is unreadable: %s" % (path, exc))
            self._columns[key] = codes
        return self._columns[key]

    # ----- row ids -------------------------------------------------------
    def save_rowids(self, tidx, rowids):
        os.makedirs(self.table_dir(tidx), exist_ok=True)
        with open(self.rowids_path(tidx), "w", encoding="utf-8") as fh:
            json.dump(rowids, fh, ensure_ascii=False)

    def load_rowids(self, tidx):
        if tidx not in self._rowids:
            try:
                with open(self.rowids_path(tidx), "r", encoding="utf-8") as fh:
                    rowids = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise DataError("prepared rowids for table %d are corrupt: %s"
                                % (tidx, exc))
            self._rowids[tidx] = rowids
        return self._rowids[tidx]

    # ----- dictionaries --------------------------------------------------
    def save_dict(self, typename, values):
        with open(self.dict_path(typename), "w", encoding="utf-8") as fh:
            json.dump(values, fh, ensure_ascii=False)

    def load_dict(self, typename):
        if typename not in self._dicts:
            try:
                with open(self.dict_path(typename), "r", encoding="utf-8") as fh:
                    values = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise DataError("prepared dictionary for %s is corrupt: %s"
                                % (typename, exc))
            if len(values) != self.domain_size(typename):
                raise DataError("prepared dictionary for %s has the wrong size"
                                % typename)
            self._dicts[typename] = values
        return self._dicts[typename]

    def decode_value(self, typename, code):
        """Decode one stored code back to its typed value (None for null)."""
        if code == self.null_code(typename):
            return None
        return self.load_dict(typename)[code]
