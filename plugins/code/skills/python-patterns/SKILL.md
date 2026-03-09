---
name: python-patterns
description: Idiomatic Python patterns and best practices
triggers:
  - Writing Python code
  - Reviewing Python code
  - Async implementation
  - Type annotation questions
---

# Python Development Patterns

Modern Python (3.10+) patterns for readable, maintainable code.

## Core Principles

1. **Readability counts** — Clear over clever
2. **Explicit over implicit** — No hidden side effects
3. **EAFP** — Easier to Ask Forgiveness than Permission

## Type Hints

### Basic Annotations

```python
def greet(name: str, times: int = 1) -> str:
    return f"Hello, {name}! " * times

def process_items(items: list[str]) -> dict[str, int]:
    return {item: len(item) for item in items}
```

### Optional and Union

```python
from typing import Optional

def find_user(user_id: int) -> Optional[User]:
    # Returns User or None
    ...

# Python 3.10+ union syntax
def parse(data: str | bytes) -> dict:
    ...
```

### Protocols (Structural Typing)

```python
from typing import Protocol

class Readable(Protocol):
    def read(self) -> str: ...

def process(source: Readable) -> str:
    return source.read().upper()

# Any class with read() method works
```

## Error Handling

### Specific Exceptions

```python
# BAD
try:
    process()
except:
    pass

# GOOD
try:
    process()
except ValueError as e:
    logger.warning(f"Invalid value: {e}")
except IOError as e:
    logger.error(f"IO failed: {e}")
    raise
```

### Exception Chaining

```python
try:
    data = fetch_data()
except HTTPError as e:
    raise DataFetchError("Failed to fetch") from e
```

### Custom Exceptions

```python
class AppError(Exception):
    """Base application error."""
    pass

class ValidationError(AppError):
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")
```

## Data Structures

### Dataclasses

```python
from dataclasses import dataclass, field

@dataclass
class User:
    name: str
    email: str
    roles: list[str] = field(default_factory=list)

    def is_admin(self) -> bool:
        return "admin" in self.roles
```

### Frozen Dataclasses

```python
@dataclass(frozen=True)
class Point:
    x: float
    y: float
```

### NamedTuple

```python
from typing import NamedTuple

class Coordinate(NamedTuple):
    lat: float
    lon: float

    def distance_to(self, other: "Coordinate") -> float:
        ...
```

## Context Managers

### With Statement

```python
with open("file.txt") as f:
    data = f.read()
# File automatically closed

# Multiple contexts
with open("in.txt") as src, open("out.txt", "w") as dst:
    dst.write(src.read())
```

### Custom Context Manager

```python
from contextlib import contextmanager

@contextmanager
def timer(name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        print(f"{name}: {elapsed:.2f}s")

with timer("processing"):
    heavy_computation()
```

## Concurrency

### Threading (I/O-bound)

```python
from concurrent.futures import ThreadPoolExecutor

def fetch_all(urls: list[str]) -> list[Response]:
    with ThreadPoolExecutor(max_workers=10) as executor:
        return list(executor.map(fetch, urls))
```

### Multiprocessing (CPU-bound)

```python
from concurrent.futures import ProcessPoolExecutor

def compute_all(items: list[Data]) -> list[Result]:
    with ProcessPoolExecutor() as executor:
        return list(executor.map(heavy_compute, items))
```

### Async/Await

```python
import asyncio
import aiohttp

async def fetch(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as response:
        return await response.text()

async def fetch_all(urls: list[str]) -> list[str]:
    async with aiohttp.ClientSession() as session:
        tasks = [fetch(session, url) for url in urls]
        return await asyncio.gather(*tasks)

# Run
results = asyncio.run(fetch_all(urls))
```

## Generators

### Memory-Efficient Processing

```python
def read_large_file(path: str):
    with open(path) as f:
        for line in f:
            yield line.strip()

# Process without loading entire file
for line in read_large_file("huge.txt"):
    process(line)
```

### Generator Expressions

```python
# List (loads all into memory)
squares = [x**2 for x in range(1_000_000)]

# Generator (lazy, memory efficient)
squares = (x**2 for x in range(1_000_000))
```

## Performance

### **slots**

```python
class Point:
    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float):
        self.x = x
        self.y = y

# ~40% less memory, faster attribute access
```

### String Joining

```python
# BAD - O(n²)
result = ""
for s in strings:
    result += s

# GOOD - O(n)
result = "".join(strings)
```

### lru_cache

```python
from functools import lru_cache

@lru_cache(maxsize=128)
def expensive_computation(n: int) -> int:
    # Cached by arguments
    ...
```

## Project Structure

```
project/
├── src/
│   └── package/
│       ├── __init__.py
│       ├── models.py
│       └── services.py
├── tests/
│   └── test_services.py
├── pyproject.toml
└── README.md
```

## Tooling

```bash
black .              # Formatting
isort .              # Import sorting
ruff check .         # Fast linting
mypy .               # Type checking
pytest --cov         # Testing with coverage
bandit -r src/       # Security scanning
```

## Anti-Patterns

| Avoid | Instead |
|-------|---------|
| `def f(items=[])` | `def f(items=None)` |
| `type(x) == str` | `isinstance(x, str)` |
| `x == None` | `x is None` |
| `from x import *` | Explicit imports |
| Bare `except:` | `except Exception:` |
| `eval(user_input)` | Never |
