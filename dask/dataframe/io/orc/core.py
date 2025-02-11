import copy

from fsspec.core import get_fs_token_paths
from fsspec.utils import stringify_path

from ....base import compute_as_if_collection, tokenize
from ....highlevelgraph import HighLevelGraph
from ....layers import DataFrameIOLayer
from ....utils import apply
from ...core import DataFrame, Scalar, new_dd_object
from .utils import ORCEngine


class ORCFunctionWrapper:
    """
    ORC Function-Wrapper Class
    Reads ORC data from disk to produce a partition.
    """

    def __init__(self, fs, columns, schema, engine, index):
        self.fs = fs
        self.columns = columns
        self.schema = schema
        self.engine = engine
        self.index = index

    def project_columns(self, columns):
        """Return a new ORCFunctionWrapper object with
        a sub-column projection.
        """
        if columns == self.columns:
            return self
        func = copy.deepcopy(self)
        func.columns = columns
        return func

    def __call__(self, parts):
        _df = self.engine.read_partition(
            self.fs,
            parts,
            self.schema,
            self.columns,
        )
        if self.index:
            _df.set_index(self.index, inplace=True)
        return _df


def _get_engine(engine):
    # Get engine
    if engine == "pyarrow":
        from .arrow import ArrowORCEngine

        return ArrowORCEngine
    elif not isinstance(engine, ORCEngine):
        raise TypeError("engine must be 'pyarrow', or an ORCEngine object")
    return engine


def read_orc(
    path,
    engine="pyarrow",
    columns=None,
    index=None,
    split_stripes=1,
    aggregate_files=None,
    storage_options=None,
):
    """Read dataframe from ORC file(s)

    Parameters
    ----------
    path: str or list(str)
        Location of file(s), which can be a full URL with protocol
        specifier, and may include glob character if a single string.
    engine: 'pyarrow' or ORCEngine
        Backend ORC engine to use for IO. Default is "pyarrow".
    columns: None or list(str)
        Columns to load. If None, loads all.
    index: str
        Column name to set as index.
    split_stripes: int or False
        Maximum number of ORC stripes to include in each output-DataFrame
        partition. Use False to specify a 1-to-1 mapping between files
        and partitions. Default is 1.
    aggregate_files : bool, default False
        Whether distinct file paths may be aggregated into the same output
        partition. A setting of True means that any two file paths may be
        aggregated into the same output partition, while False means that
        inter-file aggregation is prohibited.
    storage_options: None or dict
        Further parameters to pass to the bytes backend.

    Returns
    -------
    Dask.DataFrame (even if there is only one column)

    Examples
    --------
    >>> df = dd.read_orc('https://github.com/apache/orc/raw/'
    ...                  'master/examples/demo-11-zlib.orc')  # doctest: +SKIP
    """

    # Get engine
    engine = _get_engine(engine)

    # Process file path(s)
    storage_options = storage_options or {}
    fs, fs_token, paths = get_fs_token_paths(
        path, mode="rb", storage_options=storage_options
    )

    # Let backend engine generate a list of parts
    # from the ORC metadata.  The backend should also
    # return the schema and DataFrame-collection metadata
    parts, schema, meta = engine.read_metadata(
        fs,
        paths,
        columns,
        index,
        split_stripes,
        aggregate_files,
    )

    # Construct and return a Blockwise layer
    label = "read-orc-"
    output_name = label + tokenize(fs_token, path, columns)
    layer = DataFrameIOLayer(
        output_name,
        columns,
        parts,
        ORCFunctionWrapper(fs, columns, schema, engine, index),
        label=label,
    )
    graph = HighLevelGraph({output_name: layer}, {output_name: set()})
    return new_dd_object(graph, output_name, meta, [None] * (len(parts) + 1))


def to_orc(
    df,
    path,
    engine="pyarrow",
    write_index=True,
    storage_options=None,
    compute=True,
    compute_kwargs=None,
):
    """Store Dask.dataframe to ORC files

    Notes
    -----
    Each partition will be written to a separate file.

    Parameters
    ----------
    df : dask.dataframe.DataFrame
    path : string or pathlib.Path
        Destination directory for data.  Prepend with protocol like ``s3://``
        or ``hdfs://`` for remote data.
    engine : 'pyarrow' or ORCEngine
        Parquet library to use. If only one library is installed, it will use
        that one; if both, it will use 'fastparquet'.
    write_index : boolean, default True
        Whether or not to write the index. Defaults to True.
    storage_options : dict, default None
        Key/value pairs to be passed on to the file-system backend, if any.
    compute : bool, default True
        If True (default) then the result is computed immediately. If False
        then a ``dask.delayed`` object is returned for future computation.
    compute_kwargs : dict, default True
        Options to be passed in to the compute method

    Examples
    --------
    >>> df = dd.read_csv(...)  # doctest: +SKIP
    >>> df.to_orc('/path/to/output/', ...)  # doctest: +SKIP

    See Also
    --------
    read_orc: Read ORC data to dask.dataframe
    """

    # Get engine
    engine = _get_engine(engine)

    if hasattr(path, "name"):
        path = stringify_path(path)
    fs, _, _ = get_fs_token_paths(path, mode="wb", storage_options=storage_options)
    # Trim any protocol information from the path before forwarding
    path = fs._strip_protocol(path)

    if not write_index:
        # Not writing index - might as well drop it
        df = df.reset_index(drop=True)

    # Use df.npartitions to define file-name list
    fs.mkdirs(path, exist_ok=True)
    filenames = [f"part.{i}.orc" for i in range(df.npartitions)]

    # Construct IO graph
    dsk = {}
    name = "to-parquet-" + tokenize(
        df,
        fs,
        path,
        engine,
        write_index,
        storage_options,
    )
    part_tasks = []
    for d, filename in enumerate(filenames):
        dsk[(name, d)] = (
            apply,
            engine.write_partition,
            [
                (df._name, d),
                path,
                fs,
                filename,
            ],
        )
        part_tasks.append((name, d))
    dsk[name] = (lambda x: None, part_tasks)
    graph = HighLevelGraph.from_collections(name, dsk, dependencies=[df])

    # Compute or return future
    if compute:
        if compute_kwargs is None:
            compute_kwargs = dict()
        return compute_as_if_collection(DataFrame, graph, part_tasks, **compute_kwargs)
    return Scalar(graph, name, "")
