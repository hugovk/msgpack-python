"""Fallback pure Python implementation of msgpack"""

import sys
import array
import struct

if sys.version_info[0] == 3:
    PY3 = True
    int_types = int
    Unicode = str
    xrange = range
    def dict_iteritems(d):
        return d.items()
else:
    PY3 = False
    int_types = (int, long)
    Unicode = unicode
    def dict_iteritems(d):
        return d.iteritems()


if hasattr(sys, 'pypy_version_info'):
    # cStringIO is slow on PyPy, StringIO is faster.  However: PyPy's own
    # StringBuilder is fastest.
    from __pypy__ import newlist_hint
    try:
        from __pypy__.builders import BytesBuilder as StringBuilder
    except ImportError:
        from __pypy__.builders import StringBuilder
    USING_STRINGBUILDER = True
    class StringIO(object):
        def __init__(self, s=b''):
            if s:
                self.builder = StringBuilder(len(s))
                self.builder.append(s)
            else:
                self.builder = StringBuilder()
        def write(self, s):
            if isinstance(s, memoryview):
                s = s.tobytes()
            self.builder.append(s)
        def getvalue(self):
            return self.builder.build()
else:
    USING_STRINGBUILDER = False
    from io import BytesIO as StringIO
    newlist_hint = lambda size: []

from msgpack.exceptions import (
    BufferFull,
    OutOfData,
    UnpackValueError,
    PackValueError,
    PackOverflowError,
    ExtraData)

from msgpack import ExtType


EX_SKIP                 = 0
EX_CONSTRUCT            = 1
EX_READ_ARRAY_HEADER    = 2
EX_READ_MAP_HEADER      = 3

TYPE_IMMEDIATE          = 0
TYPE_ARRAY              = 1
TYPE_MAP                = 2
TYPE_RAW                = 3
TYPE_BIN                = 4
TYPE_EXT                = 5

DEFAULT_RECURSE_LIMIT = 511


def _check_type_strict(obj, t, type=type, tuple=tuple):
    if type(t) is tuple:
        return type(obj) in t
    else:
        return type(obj) is t


def unpack(stream, **kwargs):
    """
    Unpack an object from `stream`.

    Raises `ExtraData` when `packed` contains extra bytes.
    See :class:`Unpacker` for options.
    """
    data = stream.read()
    return unpackb(data, **kwargs)


def unpackb(packed, **kwargs):
    """
    Unpack an object from `packed`.

    Raises `ExtraData` when `packed` contains extra bytes.
    See :class:`Unpacker` for options.
    """
    unpacker = Unpacker(None, **kwargs)
    unpacker.feed(packed)
    try:
        ret = unpacker._unpack()
    except OutOfData:
        raise UnpackValueError("Data is not enough.")
    if unpacker._got_extradata():
        raise ExtraData(ret, unpacker._get_extradata())
    return ret


class Unpacker(object):
    """Streaming unpacker.

    arguments:

    :param file_like:
        File-like object having `.read(n)` method.
        If specified, unpacker reads serialized data from it and :meth:`feed()` is not usable.

    :param int read_size:
        Used as `file_like.read(read_size)`. (default: `min(16*1024, max_buffer_size)`)

    :param bool use_list:
        If true, unpack msgpack array to Python list.
        Otherwise, unpack to Python tuple. (default: True)

    :param callable object_hook:
        When specified, it should be callable.
        Unpacker calls it with a dict argument after unpacking msgpack map.
        (See also simplejson)

    :param callable object_pairs_hook:
        When specified, it should be callable.
        Unpacker calls it with a list of key-value pairs after unpacking msgpack map.
        (See also simplejson)

    :param str encoding:
        Encoding used for decoding msgpack raw.
        If it is None (default), msgpack raw is deserialized to Python bytes.

    :param str unicode_errors:
        Used for decoding msgpack raw with *encoding*.
        (default: `'strict'`)

    :param int max_buffer_size:
        Limits size of data waiting unpacked.  0 means system's INT_MAX (default).
        Raises `BufferFull` exception when it is insufficient.
        You shoud set this parameter when unpacking data from untrusted source.

    :param int max_str_len:
        Limits max length of str. (default: 2**31-1)

    :param int max_bin_len:
        Limits max length of bin. (default: 2**31-1)

    :param int max_array_len:
        Limits max length of array. (default: 2**31-1)

    :param int max_map_len:
        Limits max length of map. (default: 2**31-1)


    example of streaming deserialize from file-like object::

        unpacker = Unpacker(file_like)
        for o in unpacker:
            process(o)

    example of streaming deserialize from socket::

        unpacker = Unpacker()
        while True:
            buf = sock.recv(1024**2)
            if not buf:
                break
            unpacker.feed(buf)
            for o in unpacker:
                process(o)
    """

    def __init__(self, file_like=None, read_size=0, use_list=True,
                 object_hook=None, object_pairs_hook=None, list_hook=None,
                 encoding=None, unicode_errors='strict', max_buffer_size=0,
                 ext_hook=ExtType,
                 max_str_len=2147483647, # 2**32-1
                 max_bin_len=2147483647,
                 max_array_len=2147483647,
                 max_map_len=2147483647,
                 max_ext_len=2147483647):
        if file_like is None:
            self._feeding = True
        else:
            if not callable(file_like.read):
                raise TypeError("`file_like.read` must be callable")
            self.file_like = file_like
            self._feeding = False

        #: array of bytes feeded.
        self._buffer = b""
        #: Which position we currently reads
        self._buff_i = 0

        # When Unpacker is used as an iterable, between the calls to next(),
        # the buffer is not "consumed" completely, for efficiency sake.
        # Instead, it is done sloppily.  To make sure we raise BufferFull at
        # the correct moments, we have to keep track of how sloppy we were.
        # Furthermore, when the buffer is incomplete (that is: in the case
        # we raise an OutOfData) we need to rollback the buffer to the correct
        # state, which _buf_checkpoint records.
        self._buf_checkpoint = 0

        self._max_buffer_size = max_buffer_size or 2**31-1
        if read_size > self._max_buffer_size:
            raise ValueError("read_size must be smaller than max_buffer_size")
        self._read_size = read_size or min(self._max_buffer_size, 16*1024)
        self._encoding = encoding
        self._unicode_errors = unicode_errors
        self._use_list = use_list
        self._list_hook = list_hook
        self._object_hook = object_hook
        self._object_pairs_hook = object_pairs_hook
        self._ext_hook = ext_hook
        self._max_str_len = max_str_len
        self._max_bin_len = max_bin_len
        self._max_array_len = max_array_len
        self._max_map_len = max_map_len
        self._max_ext_len = max_ext_len

        if list_hook is not None and not callable(list_hook):
            raise TypeError('`list_hook` is not callable')
        if object_hook is not None and not callable(object_hook):
            raise TypeError('`object_hook` is not callable')
        if object_pairs_hook is not None and not callable(object_pairs_hook):
            raise TypeError('`object_pairs_hook` is not callable')
        if object_hook is not None and object_pairs_hook is not None:
            raise TypeError("object_pairs_hook and object_hook are mutually "
                            "exclusive")
        if not callable(ext_hook):
            raise TypeError("`ext_hook` is not callable")

    def feed(self, next_bytes):
        if isinstance(next_bytes, array.array):
            next_bytes = next_bytes.tostring()
        if not isinstance(next_bytes, (bytes, bytearray)):
            raise TypeError("next_bytes should be bytes, bytearray or array.array")
        assert self._feeding

        if (len(self._buffer) - self._buff_i + len(next_bytes) > self._max_buffer_size):
            raise BufferFull
        # bytes + bytearray -> bytearray
        # So cast before append
        self._buffer += bytes(next_bytes)

    def _consume(self):
        """ Gets rid of the used parts of the buffer. """
        self._buf_checkpoint = self._buff_i

    def _got_extradata(self):
        return self._buff_i < len(self._buffer)

    def _get_extradata(self):
        return self._buffer[self._buff_i:]

    def read_bytes(self, n):
        return self._read(n)

    def _read(self, n, write_bytes=None):
        # (int, Optional[Callable]) -> bytearray
        remain_bytes = len(self._buffer) - self._buff_i - n

        # Fast path: buffer has n bytes already
        if remain_bytes >= 0:
            ret = self._buffer[self._buff_i:self._buff_i+n]
            self._buff_i += n
            if write_bytes is not None:
                write_bytes(ret)
            return ret

        if self._feeding:
            self._buff_i = self._buf_checkpoint
            raise OutOfData

        # Strip buffer before checkpoint before reading file.
        if self._buf_checkpoint > 0:
            self._buffer = self._buffer[self._buf_checkpoint:]
            self._buff_i -= self._buf_checkpoint
            self._buf_checkpoint = 0

        # Read from file
        remain_bytes = -remain_bytes
        while remain_bytes > 0:
            to_read_bytes = max(self._read_size, remain_bytes)
            read_data = self.file_like.read(to_read_bytes)
            if not read_data:
                break
            assert isinstance(read_data, bytes)
            self._buffer += read_data
            remain_bytes -= len(read_data)

        if len(self._buffer) < n + self._buff_i:
            self._buff_i = 0  # rollback
            raise OutOfData

        if len(self._buffer) == n:
            # checkpoint == 0
            ret = self._buffer
            self._buffer = b""
            self._buff_i = 0
        else:
            ret = self._buffer[self._buff_i:self._buff_i+n]
            self._buff_i += n

        if write_bytes is not None:
            write_bytes(ret)
        return ret

    def _read_header(self, execute=EX_CONSTRUCT, write_bytes=None):
        typ = TYPE_IMMEDIATE
        n = 0
        obj = None
        c = self._read(1, write_bytes)
        b = ord(c)
        if   b & 0b10000000 == 0:
            obj = b
        elif b & 0b11100000 == 0b11100000:
            obj = struct.unpack("b", c)[0]
        elif b & 0b11100000 == 0b10100000:
            n = b & 0b00011111
            obj = self._read(n, write_bytes)
            typ = TYPE_RAW
            if n > self._max_str_len:
                raise UnpackValueError("%s exceeds max_str_len(%s)", n, self._max_str_len)
        elif b & 0b11110000 == 0b10010000:
            n = b & 0b00001111
            typ = TYPE_ARRAY
            if n > self._max_array_len:
                raise UnpackValueError("%s exceeds max_array_len(%s)", n, self._max_array_len)
        elif b & 0b11110000 == 0b10000000:
            n = b & 0b00001111
            typ = TYPE_MAP
            if n > self._max_map_len:
                raise UnpackValueError("%s exceeds max_map_len(%s)", n, self._max_map_len)
        elif b == 0xc0:
            obj = None
        elif b == 0xc2:
            obj = False
        elif b == 0xc3:
            obj = True
        elif b == 0xc4:
            typ = TYPE_BIN
            n = struct.unpack("B", self._read(1, write_bytes))[0]
            if n > self._max_bin_len:
                raise UnpackValueError("%s exceeds max_bin_len(%s)" % (n, self._max_bin_len))
            obj = self._read(n, write_bytes)
        elif b == 0xc5:
            typ = TYPE_BIN
            n = struct.unpack(">H", self._read(2, write_bytes))[0]
            if n > self._max_bin_len:
                raise UnpackValueError("%s exceeds max_bin_len(%s)" % (n, self._max_bin_len))
            obj = self._read(n, write_bytes)
        elif b == 0xc6:
            typ = TYPE_BIN
            n = struct.unpack(">I", self._read(4, write_bytes))[0]
            if n > self._max_bin_len:
                raise UnpackValueError("%s exceeds max_bin_len(%s)" % (n, self._max_bin_len))
            obj = self._read(n, write_bytes)
        elif b == 0xc7:  # ext 8
            typ = TYPE_EXT
            L, n = struct.unpack('Bb', self._read(2, write_bytes))
            if L > self._max_ext_len:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (L, self._max_ext_len))
            obj = self._read(L, write_bytes)
        elif b == 0xc8:  # ext 16
            typ = TYPE_EXT
            L, n = struct.unpack('>Hb', self._read(3, write_bytes))
            if L > self._max_ext_len:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (L, self._max_ext_len))
            obj = self._read(L, write_bytes)
        elif b == 0xc9:  # ext 32
            typ = TYPE_EXT
            L, n = struct.unpack('>Ib', self._read(5, write_bytes))
            if L > self._max_ext_len:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (L, self._max_ext_len))
            obj = self._read(L, write_bytes)
        elif b == 0xca:
            obj = struct.unpack(">f", self._read(4, write_bytes))[0]
        elif b == 0xcb:
            obj = struct.unpack(">d", self._read(8, write_bytes))[0]
        elif b == 0xcc:
            obj = struct.unpack("B", self._read(1, write_bytes))[0]
        elif b == 0xcd:
            obj = struct.unpack(">H", self._read(2, write_bytes))[0]
        elif b == 0xce:
            obj = struct.unpack(">I", self._read(4, write_bytes))[0]
        elif b == 0xcf:
            obj = struct.unpack(">Q", self._read(8, write_bytes))[0]
        elif b == 0xd0:
            obj = struct.unpack("b", self._read(1, write_bytes))[0]
        elif b == 0xd1:
            obj = struct.unpack(">h", self._read(2, write_bytes))[0]
        elif b == 0xd2:
            obj = struct.unpack(">i", self._read(4, write_bytes))[0]
        elif b == 0xd3:
            obj = struct.unpack(">q", self._read(8, write_bytes))[0]
        elif b == 0xd4:  # fixext 1
            typ = TYPE_EXT
            if self._max_ext_len < 1:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (1, self._max_ext_len))
            n, obj = struct.unpack('b1s', self._read(2, write_bytes))
        elif b == 0xd5:  # fixext 2
            typ = TYPE_EXT
            if self._max_ext_len < 2:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (2, self._max_ext_len))
            n, obj = struct.unpack('b2s', self._read(3, write_bytes))
        elif b == 0xd6:  # fixext 4
            typ = TYPE_EXT
            if self._max_ext_len < 4:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (4, self._max_ext_len))
            n, obj = struct.unpack('b4s', self._read(5, write_bytes))
        elif b == 0xd7:  # fixext 8
            typ = TYPE_EXT
            if self._max_ext_len < 8:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (8, self._max_ext_len))
            n, obj = struct.unpack('b8s', self._read(9, write_bytes))
        elif b == 0xd8:  # fixext 16
            typ = TYPE_EXT
            if self._max_ext_len < 16:
                raise UnpackValueError("%s exceeds max_ext_len(%s)" % (16, self._max_ext_len))
            n, obj = struct.unpack('b16s', self._read(17, write_bytes))
        elif b == 0xd9:
            typ = TYPE_RAW
            n = struct.unpack("B", self._read(1, write_bytes))[0]
            if n > self._max_str_len:
                raise UnpackValueError("%s exceeds max_str_len(%s)", n, self._max_str_len)
            obj = self._read(n, write_bytes)
        elif b == 0xda:
            typ = TYPE_RAW
            n = struct.unpack(">H", self._read(2, write_bytes))[0]
            if n > self._max_str_len:
                raise UnpackValueError("%s exceeds max_str_len(%s)", n, self._max_str_len)
            obj = self._read(n, write_bytes)
        elif b == 0xdb:
            typ = TYPE_RAW
            n = struct.unpack(">I", self._read(4, write_bytes))[0]
            if n > self._max_str_len:
                raise UnpackValueError("%s exceeds max_str_len(%s)", n, self._max_str_len)
            obj = self._read(n, write_bytes)
        elif b == 0xdc:
            n = struct.unpack(">H", self._read(2, write_bytes))[0]
            if n > self._max_array_len:
                raise UnpackValueError("%s exceeds max_array_len(%s)", n, self._max_array_len)
            typ = TYPE_ARRAY
        elif b == 0xdd:
            n = struct.unpack(">I", self._read(4, write_bytes))[0]
            if n > self._max_array_len:
                raise UnpackValueError("%s exceeds max_array_len(%s)", n, self._max_array_len)
            typ = TYPE_ARRAY
        elif b == 0xde:
            n = struct.unpack(">H", self._read(2, write_bytes))[0]
            if n > self._max_map_len:
                raise UnpackValueError("%s exceeds max_map_len(%s)", n, self._max_map_len)
            typ = TYPE_MAP
        elif b == 0xdf:
            n = struct.unpack(">I", self._read(4, write_bytes))[0]
            if n > self._max_map_len:
                raise UnpackValueError("%s exceeds max_map_len(%s)", n, self._max_map_len)
            typ = TYPE_MAP
        else:
            raise UnpackValueError("Unknown header: 0x%x" % b)
        return typ, n, obj

    def _unpack(self, execute=EX_CONSTRUCT, write_bytes=None):
        typ, n, obj = self._read_header(execute, write_bytes)

        if execute == EX_READ_ARRAY_HEADER:
            if typ != TYPE_ARRAY:
                raise UnpackValueError("Expected array")
            return n
        if execute == EX_READ_MAP_HEADER:
            if typ != TYPE_MAP:
                raise UnpackValueError("Expected map")
            return n
        # TODO should we eliminate the recursion?
        if typ == TYPE_ARRAY:
            if execute == EX_SKIP:
                for i in xrange(n):
                    # TODO check whether we need to call `list_hook`
                    self._unpack(EX_SKIP, write_bytes)
                return
            ret = newlist_hint(n)
            for i in xrange(n):
                ret.append(self._unpack(EX_CONSTRUCT, write_bytes))
            if self._list_hook is not None:
                ret = self._list_hook(ret)
            # TODO is the interaction between `list_hook` and `use_list` ok?
            return ret if self._use_list else tuple(ret)
        if typ == TYPE_MAP:
            if execute == EX_SKIP:
                for i in xrange(n):
                    # TODO check whether we need to call hooks
                    self._unpack(EX_SKIP, write_bytes)
                    self._unpack(EX_SKIP, write_bytes)
                return
            if self._object_pairs_hook is not None:
                ret = self._object_pairs_hook(
                    (self._unpack(EX_CONSTRUCT, write_bytes),
                     self._unpack(EX_CONSTRUCT, write_bytes))
                    for _ in xrange(n))
            else:
                ret = {}
                for _ in xrange(n):
                    key = self._unpack(EX_CONSTRUCT, write_bytes)
                    ret[key] = self._unpack(EX_CONSTRUCT, write_bytes)
                if self._object_hook is not None:
                    ret = self._object_hook(ret)
            return ret
        if execute == EX_SKIP:
            return
        if typ == TYPE_RAW:
            if self._encoding is not None:
                obj = obj.decode(self._encoding, self._unicode_errors)
            return obj
        if typ == TYPE_EXT:
            return self._ext_hook(n, obj)
        if typ == TYPE_BIN:
            return obj
        assert typ == TYPE_IMMEDIATE
        return obj

    def __iter__(self):
        return self

    def __next__(self):
        try:
            ret = self._unpack(EX_CONSTRUCT, None)
            self._consume()
            return ret
        except OutOfData:
            self._consume()
            raise StopIteration

    next = __next__

    def skip(self, write_bytes=None):
        self._unpack(EX_SKIP, write_bytes)
        self._consume()

    def unpack(self, write_bytes=None):
        ret = self._unpack(EX_CONSTRUCT, write_bytes)
        self._consume()
        return ret

    def read_array_header(self, write_bytes=None):
        ret = self._unpack(EX_READ_ARRAY_HEADER, write_bytes)
        self._consume()
        return ret

    def read_map_header(self, write_bytes=None):
        ret = self._unpack(EX_READ_MAP_HEADER, write_bytes)
        self._consume()
        return ret


class Packer(object):
    """
    MessagePack Packer

    usage:

        packer = Packer()
        astream.write(packer.pack(a))
        astream.write(packer.pack(b))

    Packer's constructor has some keyword arguments:

    :param callable default:
        Convert user type to builtin type that Packer supports.
        See also simplejson's document.
    :param str encoding:
            Convert unicode to bytes with this encoding. (default: 'utf-8')
    :param str unicode_errors:
        Error handler for encoding unicode. (default: 'strict')
    :param bool use_single_float:
        Use single precision float type for float. (default: False)
    :param bool autoreset:
        Reset buffer after each pack and return it's content as `bytes`. (default: True).
        If set this to false, use `bytes()` to get content and `.reset()` to clear buffer.
    :param bool use_bin_type:
        Use bin type introduced in msgpack spec 2.0 for bytes.
        It also enable str8 type for unicode.
    :param bool strict_types:
        If set to true, types will be checked to be exact. Derived classes
        from serializeable types will not be serialized and will be
        treated as unsupported type and forwarded to default.
        Additionally tuples will not be serialized as lists.
        This is useful when trying to implement accurate serialization
        for python types.
    """
    def __init__(self, default=None, encoding='utf-8', unicode_errors='strict',
                 use_single_float=False, autoreset=True, use_bin_type=False,
                 strict_types=False):
        self._strict_types = strict_types
        self._use_float = use_single_float
        self._autoreset = autoreset
        self._use_bin_type = use_bin_type
        self._encoding = encoding
        self._unicode_errors = unicode_errors
        self._buffer = StringIO()
        if default is not None:
            if not callable(default):
                raise TypeError("default must be callable")
        self._default = default

    def _pack(self, obj, nest_limit=DEFAULT_RECURSE_LIMIT,
              check=isinstance, check_type_strict=_check_type_strict):
        default_used = False
        if self._strict_types:
            check = check_type_strict
            list_types = list
        else:
            list_types = (list, tuple)
        while True:
            if nest_limit < 0:
                raise PackValueError("recursion limit exceeded")
            if obj is None:
                return self._buffer.write(b"\xc0")
            if check(obj, bool):
                if obj:
                    return self._buffer.write(b"\xc3")
                return self._buffer.write(b"\xc2")
            if check(obj, int_types):
                if 0 <= obj < 0x80:
                    return self._buffer.write(struct.pack("B", obj))
                if -0x20 <= obj < 0:
                    return self._buffer.write(struct.pack("b", obj))
                if 0x80 <= obj <= 0xff:
                    return self._buffer.write(struct.pack("BB", 0xcc, obj))
                if -0x80 <= obj < 0:
                    return self._buffer.write(struct.pack(">Bb", 0xd0, obj))
                if 0xff < obj <= 0xffff:
                    return self._buffer.write(struct.pack(">BH", 0xcd, obj))
                if -0x8000 <= obj < -0x80:
                    return self._buffer.write(struct.pack(">Bh", 0xd1, obj))
                if 0xffff < obj <= 0xffffffff:
                    return self._buffer.write(struct.pack(">BI", 0xce, obj))
                if -0x80000000 <= obj < -0x8000:
                    return self._buffer.write(struct.pack(">Bi", 0xd2, obj))
                if 0xffffffff < obj <= 0xffffffffffffffff:
                    return self._buffer.write(struct.pack(">BQ", 0xcf, obj))
                if -0x8000000000000000 <= obj < -0x80000000:
                    return self._buffer.write(struct.pack(">Bq", 0xd3, obj))
                if not default_used and self._default is not None:
                    obj = self._default(obj)
                    default_used = True
                    continue
                raise PackOverflowError("Integer value out of range")
            if check(obj, bytes):
                n = len(obj)
                if n >= 2**32:
                    raise PackValueError("Bytes is too large")
                self._pack_bin_header(n)
                return self._buffer.write(obj)
            if check(obj, Unicode):
                if self._encoding is None:
                    raise TypeError(
                        "Can't encode unicode string: "
                        "no encoding is specified")
                obj = obj.encode(self._encoding, self._unicode_errors)
                n = len(obj)
                if n >= 2**32:
                    raise PackValueError("String is too large")
                self._pack_raw_header(n)
                return self._buffer.write(obj)
            if check(obj, memoryview):
                n = len(obj) * obj.itemsize
                if n >= 2**32:
                    raise PackValueError("Memoryview is too large")
                self._pack_bin_header(n)
                return self._buffer.write(obj)
            if check(obj, float):
                if self._use_float:
                    return self._buffer.write(struct.pack(">Bf", 0xca, obj))
                return self._buffer.write(struct.pack(">Bd", 0xcb, obj))
            if check(obj, ExtType):
                code = obj.code
                data = obj.data
                assert isinstance(code, int)
                assert isinstance(data, bytes)
                L = len(data)
                if L == 1:
                    self._buffer.write(b'\xd4')
                elif L == 2:
                    self._buffer.write(b'\xd5')
                elif L == 4:
                    self._buffer.write(b'\xd6')
                elif L == 8:
                    self._buffer.write(b'\xd7')
                elif L == 16:
                    self._buffer.write(b'\xd8')
                elif L <= 0xff:
                    self._buffer.write(struct.pack(">BB", 0xc7, L))
                elif L <= 0xffff:
                    self._buffer.write(struct.pack(">BH", 0xc8, L))
                else:
                    self._buffer.write(struct.pack(">BI", 0xc9, L))
                self._buffer.write(struct.pack("b", code))
                self._buffer.write(data)
                return
            if check(obj, list_types):
                n = len(obj)
                self._pack_array_header(n)
                for i in xrange(n):
                    self._pack(obj[i], nest_limit - 1)
                return
            if check(obj, dict):
                return self._pack_map_pairs(len(obj), dict_iteritems(obj),
                                               nest_limit - 1)
            if not default_used and self._default is not None:
                obj = self._default(obj)
                default_used = 1
                continue
            raise TypeError("Cannot serialize %r" % obj)

    def pack(self, obj):
        self._pack(obj)
        ret = self._buffer.getvalue()
        if self._autoreset:
            self._buffer = StringIO()
        elif USING_STRINGBUILDER:
            self._buffer = StringIO(ret)
        return ret

    def pack_map_pairs(self, pairs):
        self._pack_map_pairs(len(pairs), pairs)
        ret = self._buffer.getvalue()
        if self._autoreset:
            self._buffer = StringIO()
        elif USING_STRINGBUILDER:
            self._buffer = StringIO(ret)
        return ret

    def pack_array_header(self, n):
        if n >= 2**32:
            raise PackValueError
        self._pack_array_header(n)
        ret = self._buffer.getvalue()
        if self._autoreset:
            self._buffer = StringIO()
        elif USING_STRINGBUILDER:
            self._buffer = StringIO(ret)
        return ret

    def pack_map_header(self, n):
        if n >= 2**32:
            raise PackValueError
        self._pack_map_header(n)
        ret = self._buffer.getvalue()
        if self._autoreset:
            self._buffer = StringIO()
        elif USING_STRINGBUILDER:
            self._buffer = StringIO(ret)
        return ret

    def pack_ext_type(self, typecode, data):
        if not isinstance(typecode, int):
            raise TypeError("typecode must have int type.")
        if not 0 <= typecode <= 127:
            raise ValueError("typecode should be 0-127")
        if not isinstance(data, bytes):
            raise TypeError("data must have bytes type")
        L = len(data)
        if L > 0xffffffff:
            raise PackValueError("Too large data")
        if L == 1:
            self._buffer.write(b'\xd4')
        elif L == 2:
            self._buffer.write(b'\xd5')
        elif L == 4:
            self._buffer.write(b'\xd6')
        elif L == 8:
            self._buffer.write(b'\xd7')
        elif L == 16:
            self._buffer.write(b'\xd8')
        elif L <= 0xff:
            self._buffer.write(b'\xc7' + struct.pack('B', L))
        elif L <= 0xffff:
            self._buffer.write(b'\xc8' + struct.pack('>H', L))
        else:
            self._buffer.write(b'\xc9' + struct.pack('>I', L))
        self._buffer.write(struct.pack('B', typecode))
        self._buffer.write(data)

    def _pack_array_header(self, n):
        if n <= 0x0f:
            return self._buffer.write(struct.pack('B', 0x90 + n))
        if n <= 0xffff:
            return self._buffer.write(struct.pack(">BH", 0xdc, n))
        if n <= 0xffffffff:
            return self._buffer.write(struct.pack(">BI", 0xdd, n))
        raise PackValueError("Array is too large")

    def _pack_map_header(self, n):
        if n <= 0x0f:
            return self._buffer.write(struct.pack('B', 0x80 + n))
        if n <= 0xffff:
            return self._buffer.write(struct.pack(">BH", 0xde, n))
        if n <= 0xffffffff:
            return self._buffer.write(struct.pack(">BI", 0xdf, n))
        raise PackValueError("Dict is too large")

    def _pack_map_pairs(self, n, pairs, nest_limit=DEFAULT_RECURSE_LIMIT):
        self._pack_map_header(n)
        for (k, v) in pairs:
            self._pack(k, nest_limit - 1)
            self._pack(v, nest_limit - 1)

    def _pack_raw_header(self, n):
        if n <= 0x1f:
            self._buffer.write(struct.pack('B', 0xa0 + n))
        elif self._use_bin_type and n <= 0xff:
            self._buffer.write(struct.pack('>BB', 0xd9, n))
        elif n <= 0xffff:
            self._buffer.write(struct.pack(">BH", 0xda, n))
        elif n <= 0xffffffff:
            self._buffer.write(struct.pack(">BI", 0xdb, n))
        else:
            raise PackValueError('Raw is too large')

    def _pack_bin_header(self, n):
        if not self._use_bin_type:
            return self._pack_raw_header(n)
        elif n <= 0xff:
            return self._buffer.write(struct.pack('>BB', 0xc4, n))
        elif n <= 0xffff:
            return self._buffer.write(struct.pack(">BH", 0xc5, n))
        elif n <= 0xffffffff:
            return self._buffer.write(struct.pack(">BI", 0xc6, n))
        else:
            raise PackValueError('Bin is too large')

    def bytes(self):
        return self._buffer.getvalue()

    def reset(self):
        self._buffer = StringIO()
