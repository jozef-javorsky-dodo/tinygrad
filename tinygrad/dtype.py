from __future__ import annotations
from typing import Final, Optional, ClassVar, Union, Callable, Literal
import math, struct, ctypes, functools
from dataclasses import dataclass, fields
from tinygrad.helpers import getenv, prod

ConstType = Union[float, int, bool]

FmtStr = Literal['?', 'b', 'B', 'h', 'H', 'i', 'I', 'q', 'Q', 'e', 'f', 'd']

# all DTypes should only be created once
class DTypeMetaClass(type):
  dcache: dict[tuple, DType] = {}
  def __call__(cls, *args, **kwargs):
    if (ret:=DTypeMetaClass.dcache.get(args, None)) is not None: return ret
    DTypeMetaClass.dcache[args] = ret = super().__call__(*args)
    return ret

@dataclass(frozen=True, eq=False)
class DType(metaclass=DTypeMetaClass):
  priority: int  # this determines when things get upcasted
  itemsize: int
  name: str
  fmt: Optional[FmtStr]
  count: int
  _scalar: Optional[DType]
  @staticmethod
  def new(priority:int, itemsize:int, name:str, fmt:Optional[FmtStr]): return DType(priority, itemsize, name, fmt, 1, None)
  def __reduce__(self): return type(self), tuple(getattr(self, f.name) for f in fields(self))
  def __repr__(self): return f"dtypes.{INVERSE_DTYPES_DICT[self.scalar().name]}"+(f".vec({self.count})" if self.count > 1 else "")
  def __lt__(self, o:DType): return (self.priority, self.itemsize, self.name, self.fmt, self.count) < (o.priority, o.itemsize, o.name, o.fmt, o.count)
  @property
  def base(self): return self
  @property
  def vcount(self): return self.count
  @functools.cache  # pylint: disable=method-cache-max-size-none
  def vec(self, sz:int) -> DType:
    assert self.count == 1, f"can't vectorize {self} with size {sz}"
    if sz == 1 or self == dtypes.void: return self  # void doesn't vectorize, and sz=1 is scalar
    return DType(self.priority, self.itemsize*sz, f"{INVERSE_DTYPES_DICT[self.name]}{sz}", None, sz, self)
  def ptr(self, size=-1, local=False) -> PtrDType:
    return PtrDType(self.priority, self.itemsize, self.name, self.fmt, self.count, None, self, local, 1, size)
  def scalar(self) -> DType: return self._scalar if self._scalar is not None else self

@dataclass(frozen=True, eq=False)
class PtrDType(DType):
  _base: DType
  local: bool
  v: int
  size: int = -1  # -1 is unlimited size
  @property
  def base(self): return self._base
  @functools.cache  # pylint: disable=method-cache-max-size-none
  def vec(self, sz:int) -> DType:
    assert self.v == 1, f"can't vectorize ptr {self} with size {sz}"
    if sz == 1: return self  # sz=1 is a scalar
    if isinstance(self, ImageDType):
      return ImageDType(self.priority, self.itemsize, self.name, self.fmt, self.count, self, self._base, self.local, sz, self.size, self.shape)
    return type(self)(self.priority, self.itemsize, self.name, self.fmt, self.count, self, self._base, self.local, sz, self.size)
  def ptr(self, size=-1, local=False): raise RuntimeError("can't make a pointer from a pointer")
  @property
  def vcount(self): return self.v
  def __repr__(self):
    return f"{self.base.__repr__()}.ptr({self.size}{', local=True' if self.local else ''})" + (f'.vec({self.v})' if self.v != 1 else '')

@dataclass(frozen=True, eq=False)
class ImageDType(PtrDType):
  shape: tuple[int, ...] = ()   # shape of the Image
  def ptr(self, size=-1, local=False) -> PtrDType:
    assert not local, "images can't be local"
    return self
  def __repr__(self): return f"dtypes.{self.name}({self.shape})" + (f'.vec({self.v})' if self.v != 1 else '')

class dtypes:
  @staticmethod
  @functools.cache
  def is_float(x: DType) -> bool: return x.scalar() in dtypes.floats or isinstance(x, ImageDType)
  @staticmethod # static methods on top, or bool in the type info will refer to dtypes.bool
  @functools.cache
  def is_int(x: DType) -> bool: return x.scalar() in dtypes.ints
  @staticmethod
  @functools.cache
  def is_unsigned(x: DType) -> bool: return x.scalar() in dtypes.uints
  @staticmethod
  def is_bool(x: DType) -> bool: return x.scalar() == dtypes.bool
  @staticmethod
  def from_py(x) -> DType:
    if x.__class__ is float: return dtypes.default_float
    if x.__class__ is int: return dtypes.default_int
    if x.__class__ is bool: return dtypes.bool
    # put this in the last is faster because there are more items than lists/tuples to check
    if x.__class__ is list or x.__class__ is tuple: return max(dtypes.from_py(xi) for xi in x) if x else dtypes.default_float
    raise RuntimeError(f"Could not infer dtype of {x} with type {type(x)}")
  @staticmethod
  def as_const(val: tuple[ConstType, ...]|ConstType, dtype:DType):
    if isinstance(val, tuple):
      assert len(val) == dtype.count, f"mismatch {val} {dtype}"
      return tuple(dtypes.as_const(x, dtype) for x in val)
    # TODO: should truncate here
    return int(val) if dtypes.is_int(dtype) else float(val) if dtypes.is_float(dtype) else bool(val)
  @staticmethod
  @functools.cache
  def min(dtype:DType):
    if dtypes.is_int(dtype): return 0 if dtypes.is_unsigned(dtype) else -2**(dtype.itemsize*8-1)
    return -float("inf") if dtypes.is_float(dtype) else False
  @staticmethod
  @functools.cache
  def max(dtype:DType):
    if dtypes.is_int(dtype): return 2**(dtype.itemsize*8)-1+dtypes.min(dtype)
    return float("inf") if dtypes.is_float(dtype) else True
  @staticmethod
  def finfo(dtype:DType) -> tuple[int, int]:
    """(exponent, mantissa)"""
    if not dtypes.is_float(dtype): raise ValueError(f"{dtype} is not a floating point type")
    return {dtypes.float16: (5, 10), dtypes.bfloat16: (8, 7), dtypes.float32: (8, 23), dtypes.float64: (11, 52),
            dtypes.fp8e5m2: (5, 2), dtypes.fp8e4m3: (4, 3)}[dtype]
  @staticmethod
  def fields() -> dict[str, DType]: return DTYPES_DICT
  void: Final[DType] = DType.new(-1, 0, "void", None)
  bool: Final[DType] = DType.new(0, 1, "bool", '?')
  int8: Final[DType] = DType.new(1, 1, "signed char", 'b')
  uint8: Final[DType] = DType.new(2, 1, "unsigned char", 'B')
  int16: Final[DType] = DType.new(3, 2, "short", 'h')
  uint16: Final[DType] = DType.new(4, 2, "unsigned short", 'H')
  int32: Final[DType] = DType.new(5, 4, "int", 'i')
  uint32: Final[DType] = DType.new(6, 4, "unsigned int", 'I')
  int64: Final[DType] = DType.new(7, 8, "long", 'q')
  uint64: Final[DType] = DType.new(8, 8, "unsigned long", 'Q')
  fp8e4m3: Final[DType] = DType.new(9, 1, "float8_e4m3", None)
  fp8e5m2: Final[DType] = DType.new(10, 1, "float8_e5m2", None)
  float16: Final[DType] = DType.new(11, 2, "half", 'e')
  # bfloat16 has higher priority than float16, so least_upper_dtype(dtypes.int64, dtypes.uint64) = dtypes.float16
  bfloat16: Final[DType] = DType.new(12, 2, "__bf16", None)
  float32: Final[DType] = DType.new(13, 4, "float", 'f')
  float64: Final[DType] = DType.new(14, 8, "double", 'd')

  # dtype aliases
  half = float16; float = float32; double = float64 # noqa: E702
  uchar = uint8; ushort = uint16; uint = uint32; ulong = uint64 # noqa: E702
  char = int8; short = int16; int = int32; long = int64 # noqa: E702

  # NOTE: these are image dtypes
  @staticmethod
  def imageh(shp): return ImageDType(100, 2, "imageh", 'e', 1, None, dtypes.float32, False, 1, prod(shp), shp)
  @staticmethod
  def imagef(shp): return ImageDType(100, 4, "imagef", 'f', 1, None, dtypes.float32, False, 1, prod(shp), shp)

  default_float: ClassVar[DType] = float32
  default_int: ClassVar[DType] = int32

  fp8s = (fp8e4m3, fp8e5m2)
  floats = fp8s + (float16, bfloat16, float32, float64)
  uints = (uint8, uint16, uint32, uint64)
  sints = (int8, int16, int32, int64)
  ints = uints + sints
  all = floats + ints + (bool,)

if (env_default_float := getenv("DEFAULT_FLOAT", "")):
  dtypes.default_float = getattr(dtypes, env_default_float.lower())
  assert dtypes.is_float(dtypes.default_float), f"{env_default_float} is not a float dtype"

DTypeLike = Union[str, DType]
def to_dtype(dtype:DTypeLike) -> DType: return dtype if isinstance(dtype, DType) else getattr(dtypes, dtype.lower())

# https://jax.readthedocs.io/en/latest/jep/9407-type-promotion.html
# we don't support weak type and complex type
promo_lattice = { dtypes.bool: [dtypes.int8, dtypes.uint8], dtypes.int8: [dtypes.int16], dtypes.int16: [dtypes.int32], dtypes.int32: [dtypes.int64],
  dtypes.int64: [dtypes.float16, dtypes.bfloat16], dtypes.uint8: [dtypes.int16, dtypes.uint16], dtypes.uint16: [dtypes.int32, dtypes.uint32],
  dtypes.uint32: [dtypes.int64, dtypes.uint64], dtypes.uint64: [dtypes.float16, dtypes.bfloat16],
  dtypes.fp8e5m2: [dtypes.float16, dtypes.bfloat16], dtypes.fp8e4m3: [dtypes.float16, dtypes.bfloat16],
  dtypes.float16: [dtypes.float32], dtypes.bfloat16: [dtypes.float32], dtypes.float32: [dtypes.float64], }

@functools.cache
def _get_recursive_parents(dtype:DType) -> set[DType]:
  return set.union(*[_get_recursive_parents(d) for d in promo_lattice[dtype]], {dtype}) if dtype != dtypes.float64 else {dtypes.float64}
@functools.cache
def least_upper_dtype(*ds:DType) -> DType:
  return min(set.intersection(*[_get_recursive_parents(d) for d in ds])) if not (images:=[d for d in ds if isinstance(d, ImageDType)]) else images[0]
def least_upper_float(dt:DType) -> DType: return dt if dtypes.is_float(dt) else least_upper_dtype(dt, dtypes.default_float)

DTYPES_DICT = {k: v for k, v in dtypes.__dict__.items() if isinstance(v, DType) and not k.startswith(("default", "void"))}
INVERSE_DTYPES_DICT = {**{v.name:k for k,v in DTYPES_DICT.items()}, "void": "void"}

def sum_acc_dtype(dt:DType):
  # default acc dtype for sum
  if dtypes.is_unsigned(dt): return least_upper_dtype(dt, dtypes.uint)
  if dtypes.is_int(dt) or dt == dtypes.bool: return least_upper_dtype(dt, dtypes.int)
  return least_upper_dtype(dt, to_dtype(getenv("SUM_DTYPE", "float32")))

def truncate_fp16(x):
  try: return struct.unpack("@e", struct.pack("@e", float(x)))[0]
  except OverflowError: return math.copysign(math.inf, x)

def truncate_bf16(x):
  max_bf16 = struct.unpack('f', struct.pack('I', 0x7f7f0000))[0]
  if abs(x) > max_bf16: return math.copysign(math.inf, x)
  f32_int = struct.unpack('I', struct.pack('f', x))[0]
  bf = struct.unpack('f', struct.pack('I', f32_int & 0xFFFF0000))[0]
  return bf

truncate: dict[DType, Callable] = {dtypes.bool: bool,
  dtypes.float16: truncate_fp16, dtypes.bfloat16: truncate_bf16,
  dtypes.float32: lambda x: ctypes.c_float(x).value, dtypes.float64: lambda x: ctypes.c_double(x).value,
  dtypes.uint8: lambda x: ctypes.c_uint8(x).value, dtypes.uint16: lambda x: ctypes.c_uint16(x).value,
  dtypes.uint32: lambda x: ctypes.c_uint32(x).value, dtypes.uint64: lambda x: ctypes.c_uint64(x).value,
  dtypes.int8: lambda x: ctypes.c_int8(x).value, dtypes.int16: lambda x: ctypes.c_int16(x).value, dtypes.int32: lambda x: ctypes.c_int32(x).value,
  dtypes.int64: lambda x: ctypes.c_int64(x).value}

# numpy and torch dtype interop

def _to_np_dtype(dtype:DType) -> Optional[type]:
  import numpy as np
  return np.dtype(dtype.fmt).type if dtype.fmt is not None else None
def _from_np_dtype(npdtype:'np.dtype') -> DType: # type: ignore [name-defined] # noqa: F821
  import numpy as np
  return dtypes.fields()[np.dtype(npdtype).name]

@functools.cache
def _to_torch_dtype(dtype:DType) -> Optional['torch.dtype']:  # type: ignore [name-defined] # noqa: F821
  import numpy as np, torch
  # NOTE: torch doesn't expose this mapping with a stable API
  try: return torch.from_numpy(np.array([], dtype=_to_np_dtype(dtype))).dtype
  except TypeError: return None
@functools.cache
def _from_torch_dtype(torchdtype:'torch.dtype') -> DType: # type: ignore [name-defined] # noqa: F821
  return {v:k for k in dtypes.all if (v:=_to_torch_dtype(k)) is not None}[torchdtype]