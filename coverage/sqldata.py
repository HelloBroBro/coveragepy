# Licensed under the Apache License: http://www.apache.org/licenses/LICENSE-2.0
# For details: https://github.com/nedbat/coveragepy/blob/master/NOTICE.txt

"""SQLite coverage data."""

from __future__ import annotations

import collections
import datetime
import functools
import glob
import itertools
import os
import random
import socket
import sqlite3
import string
import sys
import textwrap
import threading
import zlib

from typing import (
    cast, Any, Callable, Collection, Mapping,
    Sequence,
)

from coverage.debug import NoDebugging, auto_repr
from coverage.exceptions import CoverageException, DataError
from coverage.misc import file_be_gone, isolate_module
from coverage.numbits import numbits_to_nums, numbits_union, nums_to_numbits
from coverage.sqlitedb import SqliteDb
from coverage.types import AnyCallable, FilePath, TArc, TDebugCtl, TLineNo, TWarnFn
from coverage.version import __version__

os = isolate_module(os)

# If you change the schema: increment the SCHEMA_VERSION and update the
# docs in docs/dbschema.rst by running "make cogdoc".

SCHEMA_VERSION = 7

# Schema versions:
# 1: Released in 5.0a2
# 2: Added contexts in 5.0a3.
# 3: Replaced line table with line_map table.
# 4: Changed line_map.bitmap to line_map.numbits.
# 5: Added foreign key declarations.
# 6: Key-value in meta.
# 7: line_map -> line_bits

SCHEMA = """\
CREATE TABLE coverage_schema (
    -- One row, to record the version of the schema in this db.
    version integer
);

CREATE TABLE meta (
    -- Key-value pairs, to record metadata about the data
    key text,
    value text,
    unique (key)
    -- Possible keys:
    --  'has_arcs' boolean      -- Is this data recording branches?
    --  'sys_argv' text         -- The coverage command line that recorded the data.
    --  'version' text          -- The version of coverage.py that made the file.
    --  'when' text             -- Datetime when the file was created.
);

CREATE TABLE file (
    -- A row per file measured.
    id integer primary key,
    path text,
    unique (path)
);

CREATE TABLE context (
    -- A row per context measured.
    id integer primary key,
    context text,
    unique (context)
);

CREATE TABLE line_bits (
    -- If recording lines, a row per context per file executed.
    -- All of the line numbers for that file/context are in one numbits.
    file_id integer,            -- foreign key to `file`.
    context_id integer,         -- foreign key to `context`.
    numbits blob,               -- see the numbits functions in coverage.numbits
    foreign key (file_id) references file (id),
    foreign key (context_id) references context (id),
    unique (file_id, context_id)
);

CREATE TABLE arc (
    -- If recording branches, a row per context per from/to line transition executed.
    file_id integer,            -- foreign key to `file`.
    context_id integer,         -- foreign key to `context`.
    fromno integer,             -- line number jumped from.
    tono integer,               -- line number jumped to.
    foreign key (file_id) references file (id),
    foreign key (context_id) references context (id),
    unique (file_id, context_id, fromno, tono)
);

CREATE TABLE tracer (
    -- A row per file indicating the tracer used for that file.
    file_id integer primary key,
    tracer text,
    foreign key (file_id) references file (id)
);
"""

def _locked(method: AnyCallable) -> AnyCallable:
    """A decorator for methods that should hold self._lock."""
    @functools.wraps(method)
    def _wrapped(self: CoverageData, *args: Any, **kwargs: Any) -> Any:
        if self._debug.should("lock"):
            self._debug.write(f"Locking {self._lock!r} for {method.__name__}")
        with self._lock:
            if self._debug.should("lock"):
                self._debug.write(f"Locked  {self._lock!r} for {method.__name__}")
            return method(self, *args, **kwargs)
    return _wrapped


class CoverageData:
    """Manages collected coverage data, including file storage.

    This class is the public supported API to the data that coverage.py
    collects during program execution.  It includes information about what code
    was executed. It does not include information from the analysis phase, to
    determine what lines could have been executed, or what lines were not
    executed.

    .. note::

        The data file is currently a SQLite database file, with a
        :ref:`documented schema <dbschema>`. The schema is subject to change
        though, so be careful about querying it directly. Use this API if you
        can to isolate yourself from changes.

    There are a number of kinds of data that can be collected:

    * **lines**: the line numbers of source lines that were executed.
      These are always available.

    * **arcs**: pairs of source and destination line numbers for transitions
      between source lines.  These are only available if branch coverage was
      used.

    * **file tracer names**: the module names of the file tracer plugins that
      handled each file in the data.

    Lines, arcs, and file tracer names are stored for each source file. File
    names in this API are case-sensitive, even on platforms with
    case-insensitive file systems.

    A data file either stores lines, or arcs, but not both.

    A data file is associated with the data when the :class:`CoverageData`
    is created, using the parameters `basename`, `suffix`, and `no_disk`. The
    base name can be queried with :meth:`base_filename`, and the actual file
    name being used is available from :meth:`data_filename`.

    To read an existing coverage.py data file, use :meth:`read`.  You can then
    access the line, arc, or file tracer data with :meth:`lines`, :meth:`arcs`,
    or :meth:`file_tracer`.

    The :meth:`has_arcs` method indicates whether arc data is available.  You
    can get a set of the files in the data with :meth:`measured_files`.  As
    with most Python containers, you can determine if there is any data at all
    by using this object as a boolean value.

    The contexts for each line in a file can be read with
    :meth:`contexts_by_lineno`.

    To limit querying to certain contexts, use :meth:`set_query_context` or
    :meth:`set_query_contexts`. These will narrow the focus of subsequent
    :meth:`lines`, :meth:`arcs`, and :meth:`contexts_by_lineno` calls. The set
    of all measured context names can be retrieved with
    :meth:`measured_contexts`.

    Most data files will be created by coverage.py itself, but you can use
    methods here to create data files if you like.  The :meth:`add_lines`,
    :meth:`add_arcs`, and :meth:`add_file_tracers` methods add data, in ways
    that are convenient for coverage.py.

    To record data for contexts, use :meth:`set_context` to set a context to
    be used for subsequent :meth:`add_lines` and :meth:`add_arcs` calls.

    To add a source file without any measured data, use :meth:`touch_file`,
    or :meth:`touch_files` for a list of such files.

    Write the data to its file with :meth:`write`.

    You can clear the data in memory with :meth:`erase`.  Data for specific
    files can be removed from the database with :meth:`purge_files`.

    Two data collections can be combined by using :meth:`update` on one
    :class:`CoverageData`, passing it the other.

    Data in a :class:`CoverageData` can be serialized and deserialized with
    :meth:`dumps` and :meth:`loads`.

    The methods used during the coverage.py collection phase
    (:meth:`add_lines`, :meth:`add_arcs`, :meth:`set_context`, and
    :meth:`add_file_tracers`) are thread-safe.  Other methods may not be.

    """

    def __init__(
        self,
        basename: FilePath | None = None,
        suffix: str | bool | None = None,
        no_disk: bool = False,
        warn: TWarnFn | None = None,
        debug: TDebugCtl | None = None,
    ) -> None:
        """Create a :class:`CoverageData` object to hold coverage-measured data.

        Arguments:
            basename (str): the base name of the data file, defaulting to
                ".coverage". This can be a path to a file in another directory.
            suffix (str or bool): has the same meaning as the `data_suffix`
                argument to :class:`coverage.Coverage`.
            no_disk (bool): if True, keep all data in memory, and don't
                write any disk file.
            warn: a warning callback function, accepting a warning message
                argument.
            debug: a `DebugControl` object (optional)

        """
        self._no_disk = no_disk
        self._basename = os.path.abspath(basename or ".coverage")
        self._suffix = suffix
        self._warn = warn
        self._debug = debug or NoDebugging()

        self._choose_filename()
        # Maps filenames to row ids.
        self._file_map: dict[str, int] = {}
        # Maps thread ids to SqliteDb objects.
        self._dbs: dict[int, SqliteDb] = {}
        self._pid = os.getpid()
        # Synchronize the operations used during collection.
        self._lock = threading.RLock()

        # Are we in sync with the data file?
        self._have_used = False

        self._has_lines = False
        self._has_arcs = False

        self._current_context: str | None = None
        self._current_context_id: int | None = None
        self._query_context_ids: list[int] | None = None

    __repr__ = auto_repr

    def _choose_filename(self) -> None:
        """Set self._filename based on inited attributes."""
        if self._no_disk:
            self._filename = ":memory:"
        else:
            self._filename = self._basename
            suffix = filename_suffix(self._suffix)
            if suffix:
                self._filename += "." + suffix

    def _reset(self) -> None:
        """Reset our attributes."""
        if not self._no_disk:
            for db in self._dbs.values():
                db.close()
            self._dbs = {}
        self._file_map = {}
        self._have_used = False
        self._current_context_id = None

    def _open_db(self) -> None:
        """Open an existing db file, and read its metadata."""
        if self._debug.should("dataio"):
            self._debug.write(f"Opening data file {self._filename!r}")
        self._dbs[threading.get_ident()] = SqliteDb(self._filename, self._debug)
        self._read_db()

    def _read_db(self) -> None:
        """Read the metadata from a database so that we are ready to use it."""
        with self._dbs[threading.get_ident()] as db:
            try:
                row = db.execute_one("select version from coverage_schema")
                assert row is not None
            except Exception as exc:
                if "no such table: coverage_schema" in str(exc):
                    self._init_db(db)
                else:
                    raise DataError(
                        "Data file {!r} doesn't seem to be a coverage data file: {}".format(
                            self._filename, exc,
                        ),
                    ) from exc
            else:
                schema_version = row[0]
                if schema_version != SCHEMA_VERSION:
                    raise DataError(
                        "Couldn't use data file {!r}: wrong schema: {} instead of {}".format(
                            self._filename, schema_version, SCHEMA_VERSION,
                        ),
                    )

            row = db.execute_one("select value from meta where key = 'has_arcs'")
            if row is not None:
                self._has_arcs = bool(int(row[0]))
                self._has_lines = not self._has_arcs

            with db.execute("select id, path from file") as cur:
                for file_id, path in cur:
                    self._file_map[path] = file_id

    def _init_db(self, db: SqliteDb) -> None:
        """Write the initial contents of the database."""
        if self._debug.should("dataio"):
            self._debug.write(f"Initing data file {self._filename!r}")
        db.executescript(SCHEMA)
        db.execute_void("insert into coverage_schema (version) values (?)", (SCHEMA_VERSION,))

        # When writing metadata, avoid information that will needlessly change
        # the hash of the data file, unless we're debugging processes.
        meta_data = [
            ("version", __version__),
        ]
        if self._debug.should("process"):
            meta_data.extend([
                ("sys_argv", str(getattr(sys, "argv", None))),
                ("when", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ])
        db.executemany_void("insert or ignore into meta (key, value) values (?, ?)", meta_data)

    def _connect(self) -> SqliteDb:
        """Get the SqliteDb object to use."""
        if threading.get_ident() not in self._dbs:
            self._open_db()
        return self._dbs[threading.get_ident()]

    def __bool__(self) -> bool:
        if (threading.get_ident() not in self._dbs and not os.path.exists(self._filename)):
            return False
        try:
            with self._connect() as con:
                with con.execute("select * from file limit 1") as cur:
                    return bool(list(cur))
        except CoverageException:
            return False

    def dumps(self) -> bytes:
        """Serialize the current data to a byte string.

        The format of the serialized data is not documented. It is only
        suitable for use with :meth:`loads` in the same version of
        coverage.py.

        Note that this serialization is not what gets stored in coverage data
        files.  This method is meant to produce bytes that can be transmitted
        elsewhere and then deserialized with :meth:`loads`.

        Returns:
            A byte string of serialized data.

        .. versionadded:: 5.0

        """
        if self._debug.should("dataio"):
            self._debug.write(f"Dumping data from data file {self._filename!r}")
        with self._connect() as con:
            script = con.dump()
            return b"z" + zlib.compress(script.encode("utf-8"))

    def loads(self, data: bytes) -> None:
        """Deserialize data from :meth:`dumps`.

        Use with a newly-created empty :class:`CoverageData` object.  It's
        undefined what happens if the object already has data in it.

        Note that this is not for reading data from a coverage data file.  It
        is only for use on data you produced with :meth:`dumps`.

        Arguments:
            data: A byte string of serialized data produced by :meth:`dumps`.

        .. versionadded:: 5.0

        """
        if self._debug.should("dataio"):
            self._debug.write(f"Loading data into data file {self._filename!r}")
        if data[:1] != b"z":
            raise DataError(
                f"Unrecognized serialization: {data[:40]!r} (head of {len(data)} bytes)",
            )
        script = zlib.decompress(data[1:]).decode("utf-8")
        self._dbs[threading.get_ident()] = db = SqliteDb(self._filename, self._debug)
        with db:
            db.executescript(script)
        self._read_db()
        self._have_used = True

    def _file_id(self, filename: str, add: bool = False) -> int | None:
        """Get the file id for `filename`.

        If filename is not in the database yet, add it if `add` is True.
        If `add` is not True, return None.
        """
        if filename not in self._file_map:
            if add:
                with self._connect() as con:
                    self._file_map[filename] = con.execute_for_rowid(
                        "insert or replace into file (path) values (?)",
                        (filename,),
                    )
        return self._file_map.get(filename)

    def _context_id(self, context: str) -> int | None:
        """Get the id for a context."""
        assert context is not None
        self._start_using()
        with self._connect() as con:
            row = con.execute_one("select id from context where context = ?", (context,))
            if row is not None:
                return cast(int, row[0])
            else:
                return None

    @_locked
    def set_context(self, context: str | None) -> None:
        """Set the current context for future :meth:`add_lines` etc.

        `context` is a str, the name of the context to use for the next data
        additions.  The context persists until the next :meth:`set_context`.

        .. versionadded:: 5.0

        """
        if self._debug.should("dataop"):
            self._debug.write(f"Setting coverage context: {context!r}")
        self._current_context = context
        self._current_context_id = None

    def _set_context_id(self) -> None:
        """Use the _current_context to set _current_context_id."""
        context = self._current_context or ""
        context_id = self._context_id(context)
        if context_id is not None:
            self._current_context_id = context_id
        else:
            with self._connect() as con:
                self._current_context_id = con.execute_for_rowid(
                    "insert into context (context) values (?)",
                    (context,),
                )

    def base_filename(self) -> str:
        """The base filename for storing data.

        .. versionadded:: 5.0

        """
        return self._basename

    def data_filename(self) -> str:
        """Where is the data stored?

        .. versionadded:: 5.0

        """
        return self._filename

    @_locked
    def add_lines(self, line_data: Mapping[str, Collection[TLineNo]]) -> None:
        """Add measured line data.

        `line_data` is a dictionary mapping file names to iterables of ints::

            { filename: { line1, line2, ... }, ...}

        """
        if self._debug.should("dataop"):
            self._debug.write("Adding lines: %d files, %d lines total" % (
                len(line_data), sum(len(lines) for lines in line_data.values()),
            ))
            if self._debug.should("dataop2"):
                for filename, linenos in sorted(line_data.items()):
                    self._debug.write(f"  {filename}: {linenos}")
        self._start_using()
        self._choose_lines_or_arcs(lines=True)
        if not line_data:
            return
        with self._connect() as con:
            self._set_context_id()
            for filename, linenos in line_data.items():
                line_bits = nums_to_numbits(linenos)
                file_id = self._file_id(filename, add=True)
                query = "select numbits from line_bits where file_id = ? and context_id = ?"
                with con.execute(query, (file_id, self._current_context_id)) as cur:
                    existing = list(cur)
                if existing:
                    line_bits = numbits_union(line_bits, existing[0][0])

                con.execute_void(
                    "insert or replace into line_bits " +
                    " (file_id, context_id, numbits) values (?, ?, ?)",
                    (file_id, self._current_context_id, line_bits),
                )

    @_locked
    def add_arcs(self, arc_data: Mapping[str, Collection[TArc]]) -> None:
        """Add measured arc data.

        `arc_data` is a dictionary mapping file names to iterables of pairs of
        ints::

            { filename: { (l1,l2), (l1,l2), ... }, ...}

        """
        if self._debug.should("dataop"):
            self._debug.write("Adding arcs: %d files, %d arcs total" % (
                len(arc_data), sum(len(arcs) for arcs in arc_data.values()),
            ))
            if self._debug.should("dataop2"):
                for filename, arcs in sorted(arc_data.items()):
                    self._debug.write(f"  {filename}: {arcs}")
        self._start_using()
        self._choose_lines_or_arcs(arcs=True)
        if not arc_data:
            return
        with self._connect() as con:
            self._set_context_id()
            for filename, arcs in arc_data.items():
                if not arcs:
                    continue
                file_id = self._file_id(filename, add=True)
                data = [(file_id, self._current_context_id, fromno, tono) for fromno, tono in arcs]
                con.executemany_void(
                    "insert or ignore into arc " +
                    "(file_id, context_id, fromno, tono) values (?, ?, ?, ?)",
                    data,
                )

    def _choose_lines_or_arcs(self, lines: bool = False, arcs: bool = False) -> None:
        """Force the data file to choose between lines and arcs."""
        assert lines or arcs
        assert not (lines and arcs)
        if lines and self._has_arcs:
            if self._debug.should("dataop"):
                self._debug.write("Error: Can't add line measurements to existing branch data")
            raise DataError("Can't add line measurements to existing branch data")
        if arcs and self._has_lines:
            if self._debug.should("dataop"):
                self._debug.write("Error: Can't add branch measurements to existing line data")
            raise DataError("Can't add branch measurements to existing line data")
        if not self._has_arcs and not self._has_lines:
            self._has_lines = lines
            self._has_arcs = arcs
            with self._connect() as con:
                con.execute_void(
                    "insert or ignore into meta (key, value) values (?, ?)",
                    ("has_arcs", str(int(arcs))),
                )

    @_locked
    def add_file_tracers(self, file_tracers: Mapping[str, str]) -> None:
        """Add per-file plugin information.

        `file_tracers` is { filename: plugin_name, ... }

        """
        if self._debug.should("dataop"):
            self._debug.write("Adding file tracers: %d files" % (len(file_tracers),))
        if not file_tracers:
            return
        self._start_using()
        with self._connect() as con:
            for filename, plugin_name in file_tracers.items():
                file_id = self._file_id(filename, add=True)
                existing_plugin = self.file_tracer(filename)
                if existing_plugin:
                    if existing_plugin != plugin_name:
                        raise DataError(
                            "Conflicting file tracer name for '{}': {!r} vs {!r}".format(
                                filename, existing_plugin, plugin_name,
                            ),
                        )
                elif plugin_name:
                    con.execute_void(
                        "insert into tracer (file_id, tracer) values (?, ?)",
                        (file_id, plugin_name),
                    )

    def touch_file(self, filename: str, plugin_name: str = "") -> None:
        """Ensure that `filename` appears in the data, empty if needed.

        `plugin_name` is the name of the plugin responsible for this file.
        It is used to associate the right filereporter, etc.
        """
        self.touch_files([filename], plugin_name)

    def touch_files(self, filenames: Collection[str], plugin_name: str | None = None) -> None:
        """Ensure that `filenames` appear in the data, empty if needed.

        `plugin_name` is the name of the plugin responsible for these files.
        It is used to associate the right filereporter, etc.
        """
        if self._debug.should("dataop"):
            self._debug.write(f"Touching {filenames!r}")
        self._start_using()
        with self._connect(): # Use this to get one transaction.
            if not self._has_arcs and not self._has_lines:
                raise DataError("Can't touch files in an empty CoverageData")

            for filename in filenames:
                self._file_id(filename, add=True)
                if plugin_name:
                    # Set the tracer for this file
                    self.add_file_tracers({filename: plugin_name})

    def purge_files(self, filenames: Collection[str]) -> None:
        """Purge any existing coverage data for the given `filenames`.

        .. versionadded:: 7.2

        """
        if self._debug.should("dataop"):
            self._debug.write(f"Purging data for {filenames!r}")
        self._start_using()
        with self._connect() as con:

            if self._has_lines:
                sql = "delete from line_bits where file_id=?"
            elif self._has_arcs:
                sql = "delete from arc where file_id=?"
            else:
                raise DataError("Can't purge files in an empty CoverageData")

            for filename in filenames:
                file_id = self._file_id(filename, add=False)
                if file_id is None:
                    continue
                con.execute_void(sql, (file_id,))

    def update(
        self,
        other_data: CoverageData,
        map_path: Callable[[str], str] | None = None,
    ) -> None:
        """Update this data with data from another :class:`CoverageData`.

        If `map_path` is provided, it's a function that re-map paths to match
        the local machine's.  Note: `map_path` is None only when called
        directly from the test suite.

        """
        if self._debug.should("dataop"):
            self._debug.write("Updating with data from {!r}".format(
                getattr(other_data, "_filename", "???"),
            ))
        if self._has_lines and other_data._has_arcs:
            raise DataError("Can't combine arc data with line data")
        if self._has_arcs and other_data._has_lines:
            raise DataError("Can't combine line data with arc data")

        map_path = map_path or (lambda p: p)

        # Force the database we're writing to to exist before we start nesting contexts.
        self._start_using()

        # Collector for all arcs, lines and tracers
        other_data.read()
        with other_data._connect() as con:
            # Get files data.
            with con.execute("select path from file") as cur:
                files = {path: map_path(path) for (path,) in cur}

            # Get contexts data.
            with con.execute("select context from context") as cur:
                contexts = [context for (context,) in cur]

            # Get arc data.
            with con.execute(
                "select file.path, context.context, arc.fromno, arc.tono " +
                "from arc " +
                "inner join file on file.id = arc.file_id " +
                "inner join context on context.id = arc.context_id",
            ) as cur:
                arcs = [
                    (files[path], context, fromno, tono)
                    for (path, context, fromno, tono) in cur
                ]

            # Get line data.
            with con.execute(
                "select file.path, context.context, line_bits.numbits " +
                "from line_bits " +
                "inner join file on file.id = line_bits.file_id " +
                "inner join context on context.id = line_bits.context_id",
            ) as cur:
                lines: dict[tuple[str, str], bytes] = {}
                for path, context, numbits in cur:
                    key = (files[path], context)
                    if key in lines:
                        numbits = numbits_union(lines[key], numbits)
                    lines[key] = numbits

            # Get tracer data.
            with con.execute(
                "select file.path, tracer " +
                "from tracer " +
                "inner join file on file.id = tracer.file_id",
            ) as cur:
                tracers = {files[path]: tracer for (path, tracer) in cur}

        with self._connect() as con:
            assert con.con is not None
            con.con.isolation_level = "IMMEDIATE"

            # Get all tracers in the DB. Files not in the tracers are assumed
            # to have an empty string tracer. Since Sqlite does not support
            # full outer joins, we have to make two queries to fill the
            # dictionary.
            with con.execute("select path from file") as cur:
                this_tracers = {path: "" for path, in cur}
            with con.execute(
                "select file.path, tracer from tracer " +
                "inner join file on file.id = tracer.file_id",
            ) as cur:
                this_tracers.update({
                    map_path(path): tracer
                    for path, tracer in cur
                })

            # Create all file and context rows in the DB.
            con.executemany_void(
                "insert or ignore into file (path) values (?)",
                ((file,) for file in files.values()),
            )
            with con.execute("select id, path from file") as cur:
                file_ids = {path: id for id, path in cur}
            self._file_map.update(file_ids)
            con.executemany_void(
                "insert or ignore into context (context) values (?)",
                ((context,) for context in contexts),
            )
            with con.execute("select id, context from context") as cur:
                context_ids = {context: id for id, context in cur}

            # Prepare tracers and fail, if a conflict is found.
            # tracer_paths is used to ensure consistency over the tracer data
            # and tracer_map tracks the tracers to be inserted.
            tracer_map = {}
            for path in files.values():
                this_tracer = this_tracers.get(path)
                other_tracer = tracers.get(path, "")
                # If there is no tracer, there is always the None tracer.
                if this_tracer is not None and this_tracer != other_tracer:
                    raise DataError(
                        "Conflicting file tracer name for '{}': {!r} vs {!r}".format(
                            path, this_tracer, other_tracer,
                        ),
                    )
                tracer_map[path] = other_tracer

            # Prepare arc and line rows to be inserted by converting the file
            # and context strings with integer ids. Then use the efficient
            # `executemany()` to insert all rows at once.

            if arcs:
                self._choose_lines_or_arcs(arcs=True)

                arc_rows = (
                    (file_ids[file], context_ids[context], fromno, tono)
                    for file, context, fromno, tono in arcs
                )

                # Write the combined data.
                con.executemany_void(
                    "insert or ignore into arc " +
                    "(file_id, context_id, fromno, tono) values (?, ?, ?, ?)",
                    arc_rows,
                )

            if lines:
                self._choose_lines_or_arcs(lines=True)

                for (file, context), numbits in lines.items():
                    with con.execute(
                        "select numbits from line_bits where file_id = ? and context_id = ?",
                        (file_ids[file], context_ids[context]),
                    ) as cur:
                        existing = list(cur)
                    if existing:
                        lines[(file, context)] = numbits_union(numbits, existing[0][0])

                con.executemany_void(
                    "insert or replace into line_bits " +
                    "(file_id, context_id, numbits) values (?, ?, ?)",
                    [
                        (file_ids[file], context_ids[context], numbits)
                        for (file, context), numbits in lines.items()
                    ],
                )

            con.executemany_void(
                "insert or ignore into tracer (file_id, tracer) values (?, ?)",
                ((file_ids[filename], tracer) for filename, tracer in tracer_map.items()),
            )

        if not self._no_disk:
            # Update all internal cache data.
            self._reset()
            self.read()

    def erase(self, parallel: bool = False) -> None:
        """Erase the data in this object.

        If `parallel` is true, then also deletes data files created from the
        basename by parallel-mode.

        """
        self._reset()
        if self._no_disk:
            return
        if self._debug.should("dataio"):
            self._debug.write(f"Erasing data file {self._filename!r}")
        file_be_gone(self._filename)
        if parallel:
            data_dir, local = os.path.split(self._filename)
            local_abs_path = os.path.join(os.path.abspath(data_dir), local)
            pattern = glob.escape(local_abs_path) + ".*"
            for filename in glob.glob(pattern):
                if self._debug.should("dataio"):
                    self._debug.write(f"Erasing parallel data file {filename!r}")
                file_be_gone(filename)

    def read(self) -> None:
        """Start using an existing data file."""
        if os.path.exists(self._filename):
            with self._connect():
                self._have_used = True

    def write(self) -> None:
        """Ensure the data is written to the data file."""
        pass

    def _start_using(self) -> None:
        """Call this before using the database at all."""
        if self._pid != os.getpid():
            # Looks like we forked! Have to start a new data file.
            self._reset()
            self._choose_filename()
            self._pid = os.getpid()
        if not self._have_used:
            self.erase()
        self._have_used = True

    def has_arcs(self) -> bool:
        """Does the database have arcs (True) or lines (False)."""
        return bool(self._has_arcs)

    def measured_files(self) -> set[str]:
        """A set of all files that have been measured.

        Note that a file may be mentioned as measured even though no lines or
        arcs for that file are present in the data.

        """
        return set(self._file_map)

    def measured_contexts(self) -> set[str]:
        """A set of all contexts that have been measured.

        .. versionadded:: 5.0

        """
        self._start_using()
        with self._connect() as con:
            with con.execute("select distinct(context) from context") as cur:
                contexts = {row[0] for row in cur}
        return contexts

    def file_tracer(self, filename: str) -> str | None:
        """Get the plugin name of the file tracer for a file.

        Returns the name of the plugin that handles this file.  If the file was
        measured, but didn't use a plugin, then "" is returned.  If the file
        was not measured, then None is returned.

        """
        self._start_using()
        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return None
            row = con.execute_one("select tracer from tracer where file_id = ?", (file_id,))
            if row is not None:
                return row[0] or ""
            return ""   # File was measured, but no tracer associated.

    def set_query_context(self, context: str) -> None:
        """Set a context for subsequent querying.

        The next :meth:`lines`, :meth:`arcs`, or :meth:`contexts_by_lineno`
        calls will be limited to only one context.  `context` is a string which
        must match a context exactly.  If it does not, no exception is raised,
        but queries will return no data.

        .. versionadded:: 5.0

        """
        self._start_using()
        with self._connect() as con:
            with con.execute("select id from context where context = ?", (context,)) as cur:
                self._query_context_ids = [row[0] for row in cur.fetchall()]

    def set_query_contexts(self, contexts: Sequence[str] | None) -> None:
        """Set a number of contexts for subsequent querying.

        The next :meth:`lines`, :meth:`arcs`, or :meth:`contexts_by_lineno`
        calls will be limited to the specified contexts.  `contexts` is a list
        of Python regular expressions.  Contexts will be matched using
        :func:`re.search <python:re.search>`.  Data will be included in query
        results if they are part of any of the contexts matched.

        .. versionadded:: 5.0

        """
        self._start_using()
        if contexts:
            with self._connect() as con:
                context_clause = " or ".join(["context regexp ?"] * len(contexts))
                with con.execute("select id from context where " + context_clause, contexts) as cur:
                    self._query_context_ids = [row[0] for row in cur.fetchall()]
        else:
            self._query_context_ids = None

    def lines(self, filename: str) -> list[TLineNo] | None:
        """Get the list of lines executed for a source file.

        If the file was not measured, returns None.  A file might be measured,
        and have no lines executed, in which case an empty list is returned.

        If the file was executed, returns a list of integers, the line numbers
        executed in the file. The list is in no particular order.

        """
        self._start_using()
        if self.has_arcs():
            arcs = self.arcs(filename)
            if arcs is not None:
                all_lines = itertools.chain.from_iterable(arcs)
                return list({l for l in all_lines if l > 0})

        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return None
            else:
                query = "select numbits from line_bits where file_id = ?"
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ", ".join("?" * len(self._query_context_ids))
                    query += " and context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                with con.execute(query, data) as cur:
                    bitmaps = list(cur)
                nums = set()
                for row in bitmaps:
                    nums.update(numbits_to_nums(row[0]))
                return list(nums)

    def arcs(self, filename: str) -> list[TArc] | None:
        """Get the list of arcs executed for a file.

        If the file was not measured, returns None.  A file might be measured,
        and have no arcs executed, in which case an empty list is returned.

        If the file was executed, returns a list of 2-tuples of integers. Each
        pair is a starting line number and an ending line number for a
        transition from one line to another. The list is in no particular
        order.

        Negative numbers have special meaning.  If the starting line number is
        -N, it represents an entry to the code object that starts at line N.
        If the ending ling number is -N, it's an exit from the code object that
        starts at line N.

        """
        self._start_using()
        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return None
            else:
                query = "select distinct fromno, tono from arc where file_id = ?"
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ", ".join("?" * len(self._query_context_ids))
                    query += " and context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                with con.execute(query, data) as cur:
                    return list(cur)

    def contexts_by_lineno(self, filename: str) -> dict[TLineNo, list[str]]:
        """Get the contexts for each line in a file.

        Returns:
            A dict mapping line numbers to a list of context names.

        .. versionadded:: 5.0

        """
        self._start_using()
        with self._connect() as con:
            file_id = self._file_id(filename)
            if file_id is None:
                return {}

            lineno_contexts_map = collections.defaultdict(set)
            if self.has_arcs():
                query = (
                    "select arc.fromno, arc.tono, context.context " +
                    "from arc, context " +
                    "where arc.file_id = ? and arc.context_id = context.id"
                )
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ", ".join("?" * len(self._query_context_ids))
                    query += " and arc.context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                with con.execute(query, data) as cur:
                    for fromno, tono, context in cur:
                        if fromno > 0:
                            lineno_contexts_map[fromno].add(context)
                        if tono > 0:
                            lineno_contexts_map[tono].add(context)
            else:
                query = (
                    "select l.numbits, c.context from line_bits l, context c " +
                    "where l.context_id = c.id " +
                    "and file_id = ?"
                )
                data = [file_id]
                if self._query_context_ids is not None:
                    ids_array = ", ".join("?" * len(self._query_context_ids))
                    query += " and l.context_id in (" + ids_array + ")"
                    data += self._query_context_ids
                with con.execute(query, data) as cur:
                    for numbits, context in cur:
                        for lineno in numbits_to_nums(numbits):
                            lineno_contexts_map[lineno].add(context)

        return {lineno: list(contexts) for lineno, contexts in lineno_contexts_map.items()}

    @classmethod
    def sys_info(cls) -> list[tuple[str, Any]]:
        """Our information for `Coverage.sys_info`.

        Returns a list of (key, value) pairs.

        """
        with SqliteDb(":memory:", debug=NoDebugging()) as db:
            with db.execute("pragma temp_store") as cur:
                temp_store = [row[0] for row in cur]
            with db.execute("pragma compile_options") as cur:
                copts = [row[0] for row in cur]
            copts = textwrap.wrap(", ".join(copts), width=75)

        return [
            ("sqlite3_sqlite_version", sqlite3.sqlite_version),
            ("sqlite3_temp_store", temp_store),
            ("sqlite3_compile_options", copts),
        ]


def filename_suffix(suffix: str | bool | None) -> str | None:
    """Compute a filename suffix for a data file.

    If `suffix` is a string or None, simply return it. If `suffix` is True,
    then build a suffix incorporating the hostname, process id, and a random
    number.

    Returns a string or None.

    """
    if suffix is True:
        # If data_suffix was a simple true value, then make a suffix with
        # plenty of distinguishing information.  We do this here in
        # `save()` at the last minute so that the pid will be correct even
        # if the process forks.
        die = random.Random(os.urandom(8))
        letters = string.ascii_uppercase + string.ascii_lowercase
        rolls = "".join(die.choice(letters) for _ in range(6))
        suffix = f"{socket.gethostname()}.{os.getpid()}.X{rolls}x"
    elif suffix is False:
        suffix = None
    return suffix
