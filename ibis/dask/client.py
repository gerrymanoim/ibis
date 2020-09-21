"""The dask client implementation."""

from __future__ import absolute_import

import re
from functools import partial

import dateutil.parser
import dask.dataframe as dd
import numpy as np
import pandas as pd
import pytz
import toolz
from multipledispatch import Dispatcher
from pkg_resources import parse_version

import ibis.client as client
import ibis.common.exceptions as com
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops
import ibis.expr.schema as sch
import ibis.expr.types as ir
from ibis.compat import CategoricalDtype, DatetimeTZDtype
from ibis.dask.core import execute_and_reset

try:
    infer_dask_dtype = pd.api.types.infer_dtype
except AttributeError:
    infer_dask_dtype = pd.lib.infer_dtype


_ibis_dtypes = toolz.valmap(
    np.dtype,
    {
        dt.Boolean: np.bool_,
        dt.Null: np.object_,
        dt.Array: np.object_,
        dt.String: np.object_,
        dt.Binary: np.object_,
        dt.Date: 'datetime64[ns]',
        dt.Time: 'timedelta64[ns]',
        dt.Timestamp: 'datetime64[ns]',
        dt.Int8: np.int8,
        dt.Int16: np.int16,
        dt.Int32: np.int32,
        dt.Int64: np.int64,
        dt.UInt8: np.uint8,
        dt.UInt16: np.uint16,
        dt.UInt32: np.uint32,
        dt.UInt64: np.uint64,
        dt.Float32: np.float32,
        dt.Float64: np.float64,
        dt.Decimal: np.object_,
        dt.Struct: np.object_,
    },
)


_numpy_dtypes = toolz.keymap(
    np.dtype,
    {
        'bool': dt.boolean,
        'int8': dt.int8,
        'int16': dt.int16,
        'int32': dt.int32,
        'int64': dt.int64,
        'uint8': dt.uint8,
        'uint16': dt.uint16,
        'uint32': dt.uint32,
        'uint64': dt.uint64,
        'float16': dt.float16,
        'float32': dt.float32,
        'float64': dt.float64,
        'double': dt.double,
        'unicode': dt.string,
        'str': dt.string,
        'datetime64': dt.timestamp,
        'datetime64[ns]': dt.timestamp,
        'timedelta64': dt.interval,
        'timedelta64[ns]': dt.Interval('ns'),
    },
)


_inferable_dask_dtypes = {
    'boolean': dt.boolean,
    'string': dt.string,
    'unicode': dt.string,
    'bytes': dt.string,
    'empty': dt.string,
}


@dt.dtype.register(np.dtype)
def from_numpy_dtype(value):
    try:
        return _numpy_dtypes[value]
    except KeyError:
        raise TypeError(
            'numpy dtype {!r} is not supported in the dask backend'.format(
                value
            )
        )


@dt.dtype.register(DatetimeTZDtype)
def from_dask_tzdtype(value):
    return dt.Timestamp(timezone=str(value.tz))


@dt.dtype.register(CategoricalDtype)
def from_dask_categorical(value):
    return dt.Category()


@dt.infer.register(
    (np.generic,)
    + tuple(
        frozenset(
            np.signedinteger.__subclasses__()
            + np.unsignedinteger.__subclasses__()  # np.int64, np.uint64, etc.
        )
    )  # we need this because in Python 2 int is a parent of np.integer
)
def infer_numpy_scalar(value):
    return dt.dtype(value.dtype)


@dt.infer.register(pd.Timestamp)
def infer_dask_timestamp(value):
    if value.tz is not None:
        return dt.Timestamp(timezone=str(value.tz))
    else:
        return dt.timestamp


@dt.infer.register(np.ndarray)
def infer_array(value):
    # TODO(kszucs): infer series
    return dt.Array(dt.dtype(value.dtype.name))


@sch.schema.register(dd.Series)
def schema_from_series(s):
    return sch.schema(tuple(s.iteritems()))


@sch.infer.register(dd.DataFrame)
def infer_dask_schema(df, schema=None):
    schema = schema if schema is not None else {}

    pairs = []
    for column_name, dask_dtype in df.dtypes.iteritems():
        if not isinstance(column_name, str):
            raise TypeError(
                'Column names must be strings to use the dask backend'
            )

        if column_name in schema:
            ibis_dtype = dt.dtype(schema[column_name])
        elif dask_dtype == np.object_:
            inferred_dtype = infer_dask_dtype(df[column_name], skipna=True)
            if inferred_dtype in {'mixed', 'decimal'}:
                # TODO: in principal we can handle decimal (added in pandas
                # 0.23)
                raise TypeError(
                    'Unable to infer type of column {0!r}. Try instantiating '
                    'your table from the client with client.table('
                    "'my_table', schema={{{0!r}: <explicit type>}})".format(
                        column_name
                    )
                )
            ibis_dtype = _inferable_dask_dtypes[inferred_dtype]
        else:
            ibis_dtype = dt.dtype(dask_dtype)

        pairs.append((column_name, ibis_dtype))

    return sch.schema(pairs)


def ibis_dtype_to_dask(ibis_dtype):
    """Convert ibis dtype to the dask / numpy alternative"""
    assert isinstance(ibis_dtype, dt.DataType)

    if isinstance(ibis_dtype, dt.Timestamp) and ibis_dtype.timezone:
        return DatetimeTZDtype('ns', ibis_dtype.timezone)
    elif isinstance(ibis_dtype, dt.Interval):
        return np.dtype('timedelta64[{}]'.format(ibis_dtype.unit))
    elif isinstance(ibis_dtype, dt.Category):
        return CategoricalDtype()
    elif type(ibis_dtype) in _ibis_dtypes:
        return _ibis_dtypes[type(ibis_dtype)]
    else:
        return np.dtype(np.object_)


def ibis_schema_to_dask(schema):
    return list(zip(schema.names, map(ibis_dtype_to_dask, schema.types)))


convert = Dispatcher(
    'convert',
    doc="""\
Convert `column` to the dask dtype corresponding to `out_dtype`, where the
dtype of `column` is `in_dtype`.

Parameters
----------
in_dtype : Union[np.dtype, dask_dtype]
    The dtype of `column`, used for dispatching
out_dtype : ibis.expr.datatypes.DataType
    The requested ibis type of the output
column : dd.Series
    The column to convert

Returns
-------
result : dd.Series
    The converted column
""",
)


@convert.register(DatetimeTZDtype, dt.Timestamp, dd.Series)
def convert_datetimetz_to_timestamp(in_dtype, out_dtype, column):
    output_timezone = out_dtype.timezone
    if output_timezone is not None:
        return column.dt.tz_convert(output_timezone)
    return column.astype(out_dtype.to_dask(), errors='ignore')


def convert_timezone(obj, timezone):
    """Convert `obj` to the timezone `timezone`.

    Parameters
    ----------
    obj : datetime.date or datetime.datetime

    Returns
    -------
    type(obj)
    """
    if timezone is None:
        return obj.replace(tzinfo=None)
    return pytz.timezone(timezone).localize(obj)


DASK_STRING_TYPES = {'string', 'unicode', 'bytes'}
DASK_DATE_TYPES = {'datetime', 'datetime64', 'date'}


@convert.register(np.dtype, dt.Timestamp, dd.Series)
def convert_datetime64_to_timestamp(in_dtype, out_dtype, column):
    if in_dtype.type == np.datetime64:
        return column.astype(out_dtype.to_dask())
    try:
        series = pd.to_datetime(column, utc=True)
    except pd.errors.OutOfBoundsDatetime:
        inferred_dtype = infer_dask_dtype(column, skipna=True)
        if inferred_dtype in DASK_DATE_TYPES:
            # not great, but not really any other option
            return column.map(
                partial(convert_timezone, timezone=out_dtype.timezone)
            )
        if inferred_dtype not in DASK_STRING_TYPES:
            raise TypeError(
                (
                    'Conversion to timestamp not supported for Series of type '
                    '{!r}'
                ).format(inferred_dtype)
            )
        return column.map(dateutil.parser.parse)
    else:
        utc_dtype = DatetimeTZDtype('ns', 'UTC')
        return series.astype(utc_dtype).dt.tz_convert(out_dtype.timezone)


@convert.register(np.dtype, dt.Interval, dd.Series)
def convert_any_to_interval(_, out_dtype, column):
    return column.values.astype(out_dtype.to_dask())


@convert.register(np.dtype, dt.String, dd.Series)
def convert_any_to_string(_, out_dtype, column):
    result = column.astype(out_dtype.to_dask(), errors='ignore')
    return result


@convert.register(np.dtype, dt.Boolean, dd.Series)
def convert_boolean_to_series(in_dtype, out_dtype, column):
    # XXX: this is a workaround until #1595 can be addressed
    in_dtype_type = in_dtype.type
    out_dtype_type = out_dtype.to_dask().type
    if in_dtype_type != np.object_ and in_dtype_type != out_dtype_type:
        return column.astype(out_dtype_type)
    return column


@convert.register(object, dt.DataType, dd.Series)
def convert_any_to_any(_, out_dtype, column):
    return column.astype(out_dtype.to_dask(), errors='ignore')


def ibis_schema_apply_to(schema, df):
    """Applies the Ibis schema to a dask DataFrame

    Parameters
    ----------
    schema : ibis.schema.Schema
    df : dask.dataframe.DataFrame

    Returns
    -------
    df : dask.dataframeDataFrame

    Notes
    -----
    Mutates `df`
    """

    for column, dtype in schema.items():
        dask_dtype = dtype.to_dask()
        col = df[column]
        col_dtype = col.dtype

        try:
            not_equal = dask_dtype != col_dtype
        except TypeError:
            # ugh, we can't compare dtypes coming from dask, assume not equal
            not_equal = True

        if not_equal or isinstance(dtype, dt.String):
            df[column] = convert(col_dtype, dtype, col)

    return df


dt.DataType.to_dask = ibis_dtype_to_dask
sch.Schema.to_dask = ibis_schema_to_dask
sch.Schema.apply_to = ibis_schema_apply_to


class DaskTable(ops.DatabaseTable):
    pass


class DaskClient(client.Client):

    dialect = None  # defined in ibis.dask.api

    def __init__(self, dictionary):
        self.dictionary = dictionary

    def table(self, name, schema=None):
        df = self.dictionary[name]
        schema = sch.infer(df, schema=schema)
        return DaskTable(name, schema, self).to_expr()

    def execute(self, query, params=None, limit='default', **kwargs):
        if limit != 'default':
            raise ValueError(
                'limit parameter to execute is not yet implemented in the '
                'dask backend'
            )

        if not isinstance(query, ir.Expr):
            raise TypeError(
                "`query` has type {!r}, expected ibis.expr.types.Expr".format(
                    type(query).__name__
                )
            )
        return execute_and_reset(query, params=params, **kwargs)

    def compile(self, expr, *args, **kwargs):
        """Compile `expr`.

        Notes
        -----
        For the dask backend this is a no-op.

        """
        return expr

    def database(self, name=None):
        """Construct a database called `name`."""
        return DaskDatabase(name, self)

    def list_tables(self, like=None):
        """List the available tables."""
        tables = list(self.dictionary.keys())
        if like is not None:
            pattern = re.compile(like)
            return list(filter(lambda t: pattern.findall(t), tables))
        return tables

    def load_data(self, table_name, obj, **kwargs):
        """Load data from `obj` into `table_name`.

        Parameters
        ----------
        table_name : str
        obj : dask.dataframe.DataFrame

        """
        # kwargs is a catch all for any options required by other backends.
        self.dictionary[table_name] = obj

    def create_table(self, table_name, obj=None, schema=None):
        """Create a table."""
        if obj is None and schema is None:
            raise com.IbisError('Must pass expr or schema')

        if obj is not None:
            df = obj
        else:
            # TODO - this isn't right
            dtypes = ibis_schema_to_dask(schema)
            df = schema.apply_to(
                dd.DataFrame(columns=list(map(toolz.first, dtypes)))
            )

        self.dictionary[table_name] = df

    def get_schema(self, table_name, database=None):
        """Return a Schema object for the indicated table and database.

        Parameters
        ----------
        table_name : str
            May be fully qualified
        database : str

        Returns
        -------
        ibis.expr.schema.Schema

        """
        return sch.infer(self.dictionary[table_name])

    def exists_table(self, name):
        """Determine if the indicated table or view exists.

        Parameters
        ----------
        name : str
        database : str

        Returns
        -------
        bool

        """
        return bool(self.list_tables(like=name))

    @property
    def version(self) -> str:
        """Return the version of the underlying backend library."""
        return parse_version(dd.__version__)


class DaskDatabase(client.Database):
    pass
