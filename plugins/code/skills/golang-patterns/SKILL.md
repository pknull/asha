---
name: golang-patterns
description: Idiomatic Go patterns and best practices
triggers:
  - Writing Go code
  - Reviewing Go code
  - Designing Go packages
  - Concurrency implementation
---

# Go Development Patterns

Idiomatic Go for robust, efficient, maintainable applications.

## Core Principles

1. **Simplicity over cleverness** — Obvious code wins
2. **Make zero values useful** — Uninitialized structs should work
3. **Accept interfaces, return structs** — Flexibility in, clarity out

## Error Handling

### Wrap with Context

```go
// BAD
if err != nil {
    return err
}

// GOOD
if err != nil {
    return fmt.Errorf("fetching user %d: %w", userID, err)
}
```

### Custom Errors

```go
type ValidationError struct {
    Field   string
    Message string
}

func (e *ValidationError) Error() string {
    return fmt.Sprintf("%s: %s", e.Field, e.Message)
}

// Sentinel errors
var ErrNotFound = errors.New("resource not found")
```

### Error Checking

```go
// Check specific error
if errors.Is(err, ErrNotFound) {
    return nil // Handle gracefully
}

// Check error type
var validErr *ValidationError
if errors.As(err, &validErr) {
    log.Printf("Validation failed: %s", validErr.Field)
}
```

## Concurrency

### Worker Pool

```go
func processJobs(jobs <-chan Job, workers int) {
    var wg sync.WaitGroup
    for i := 0; i < workers; i++ {
        wg.Add(1)
        go func() {
            defer wg.Done()
            for job := range jobs {
                process(job)
            }
        }()
    }
    wg.Wait()
}
```

### Context Cancellation

```go
func FetchData(ctx context.Context, id string) (*Data, error) {
    select {
    case <-ctx.Done():
        return nil, ctx.Err()
    default:
    }

    // Proceed with fetch...
}

// Usage
ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
defer cancel()
data, err := FetchData(ctx, "123")
```

### errgroup

```go
import "golang.org/x/sync/errgroup"

func fetchAll(ctx context.Context, ids []string) error {
    g, ctx := errgroup.WithContext(ctx)

    for _, id := range ids {
        id := id // Capture loop variable
        g.Go(func() error {
            return fetch(ctx, id)
        })
    }

    return g.Wait() // Returns first error
}
```

### Graceful Shutdown

```go
func main() {
    srv := &http.Server{Addr: ":8080"}

    go func() {
        if err := srv.ListenAndServe(); err != http.ErrServerClosed {
            log.Fatal(err)
        }
    }()

    quit := make(chan os.Signal, 1)
    signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
    <-quit

    ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()
    srv.Shutdown(ctx)
}
```

## Interface Design

### Small Interfaces

```go
// GOOD - Single method
type Reader interface {
    Read(p []byte) (n int, err error)
}

// Compose larger interfaces
type ReadCloser interface {
    Reader
    Closer
}

// BAD - Too many methods
type Everything interface {
    Read() error
    Write() error
    Delete() error
    // ...20 more
}
```

### Define at Point of Use

```go
// Consumer defines what it needs
package consumer

type UserGetter interface {
    GetUser(id string) (*User, error)
}

func Process(ug UserGetter) {
    // Uses only GetUser
}
```

## Package Organization

```
project/
├── cmd/              # Executables
│   └── server/
│       └── main.go
├── internal/         # Private packages
│   ├── auth/
│   └── store/
├── pkg/              # Public packages
│   └── client/
├── api/              # API definitions
└── testdata/         # Test fixtures
```

## Struct Patterns

### Functional Options

```go
type Server struct {
    host    string
    port    int
    timeout time.Duration
}

type Option func(*Server)

func WithPort(port int) Option {
    return func(s *Server) {
        s.port = port
    }
}

func WithTimeout(d time.Duration) Option {
    return func(s *Server) {
        s.timeout = d
    }
}

func NewServer(host string, opts ...Option) *Server {
    s := &Server{
        host:    host,
        port:    8080,        // defaults
        timeout: 30 * time.Second,
    }
    for _, opt := range opts {
        opt(s)
    }
    return s
}

// Usage
srv := NewServer("localhost", WithPort(9000), WithTimeout(time.Minute))
```

### Embedding

```go
type Logger struct{}
func (l *Logger) Log(msg string) { /* ... */ }

type Service struct {
    Logger // Embedded - Service now has Log method
}

svc := &Service{}
svc.Log("hello") // Works
```

## Performance

### Preallocate Slices

```go
// BAD
var result []int
for i := 0; i < 1000; i++ {
    result = append(result, i)
}

// GOOD
result := make([]int, 0, 1000)
for i := 0; i < 1000; i++ {
    result = append(result, i)
}
```

### strings.Builder

```go
// BAD
var s string
for _, word := range words {
    s += word + " "
}

// GOOD
var b strings.Builder
for _, word := range words {
    b.WriteString(word)
    b.WriteString(" ")
}
s := b.String()
```

### sync.Pool

```go
var bufPool = sync.Pool{
    New: func() interface{} {
        return new(bytes.Buffer)
    },
}

func process() {
    buf := bufPool.Get().(*bytes.Buffer)
    defer func() {
        buf.Reset()
        bufPool.Put(buf)
    }()
    // Use buf...
}
```

## Tooling

```bash
go test -race ./...      # Detect data races
go test -cover ./...     # Coverage
go vet ./...             # Static analysis
staticcheck ./...        # Extended analysis
golangci-lint run        # All linters
```

## Anti-Patterns

| Avoid | Instead |
|-------|---------|
| `panic()` for errors | Return error |
| Context in struct | Context as first param |
| Mixed receivers | Consistent pointer or value |
| Ignored errors | Handle or `_ =` with comment |
| Global mutable state | Dependency injection |
| Naked returns (long funcs) | Explicit returns |
