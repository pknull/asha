---
description: "Go development patterns, testing, and verification workflows"
triggers:
  - "golang"
  - "go mod"
  - "go build"
---

# Go Development Skill

Comprehensive Go development guidance covering idiomatic patterns, testing, and verification.

## Project Structure

```
project/
├── cmd/
│   └── myapp/
│       └── main.go           # Entry point
├── internal/                  # Private packages
│   ├── api/
│   │   ├── handler.go
│   │   └── handler_test.go
│   ├── service/
│   │   ├── user.go
│   │   └── user_test.go
│   └── repository/
│       └── postgres.go
├── pkg/                       # Public packages (importable)
│   └── client/
│       └── client.go
├── go.mod
├── go.sum
└── Makefile
```

## Idiomatic Patterns

### Error Handling

```go
// Always wrap errors with context
func (s *UserService) GetUser(ctx context.Context, id string) (*User, error) {
    user, err := s.repo.FindByID(ctx, id)
    if err != nil {
        return nil, fmt.Errorf("get user %s: %w", id, err)
    }
    return user, nil
}

// Check error types with errors.Is/As
if errors.Is(err, ErrNotFound) {
    return nil, status.Error(codes.NotFound, "user not found")
}

// Sentinel errors for expected conditions
var ErrNotFound = errors.New("not found")
var ErrInvalidInput = errors.New("invalid input")
```

### Context Propagation

```go
// Context always first parameter
func ProcessRequest(ctx context.Context, req *Request) (*Response, error) {
    // Respect cancellation
    select {
    case <-ctx.Done():
        return nil, ctx.Err()
    default:
    }

    // Pass context to all downstream calls
    result, err := s.db.Query(ctx, query)
    if err != nil {
        return nil, err
    }

    return &Response{Data: result}, nil
}
```

### Defer for Cleanup

```go
func ReadConfig(path string) (*Config, error) {
    f, err := os.Open(path)
    if err != nil {
        return nil, fmt.Errorf("open config: %w", err)
    }
    defer f.Close()  // Always closes, even on error

    var cfg Config
    if err := json.NewDecoder(f).Decode(&cfg); err != nil {
        return nil, fmt.Errorf("decode config: %w", err)
    }
    return &cfg, nil
}
```

### Interface Design

```go
// Small interfaces, defined by consumer
type UserRepository interface {
    FindByID(ctx context.Context, id string) (*User, error)
    Save(ctx context.Context, user *User) error
}

// Accept interfaces, return structs
func NewUserService(repo UserRepository) *UserService {
    return &UserService{repo: repo}
}
```

### Concurrency

```go
// Use errgroup for managed goroutines
func FetchAll(ctx context.Context, urls []string) ([]Result, error) {
    g, ctx := errgroup.WithContext(ctx)
    results := make([]Result, len(urls))

    for i, url := range urls {
        i, url := i, url  // Capture loop variables
        g.Go(func() error {
            res, err := fetch(ctx, url)
            if err != nil {
                return err
            }
            results[i] = res
            return nil
        })
    }

    if err := g.Wait(); err != nil {
        return nil, err
    }
    return results, nil
}

// Use channels for communication
func worker(ctx context.Context, jobs <-chan Job, results chan<- Result) {
    for {
        select {
        case <-ctx.Done():
            return
        case job, ok := <-jobs:
            if !ok {
                return
            }
            results <- process(job)
        }
    }
}
```

### Options Pattern

```go
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

func NewServer(opts ...Option) *Server {
    s := &Server{
        port:    8080,
        timeout: 30 * time.Second,
    }
    for _, opt := range opts {
        opt(s)
    }
    return s
}

// Usage
srv := NewServer(WithPort(9000), WithTimeout(time.Minute))
```

---

## Testing

### Table-Driven Tests

```go
func TestUserService_Validate(t *testing.T) {
    tests := []struct {
        name    string
        input   User
        wantErr bool
    }{
        {
            name:    "valid user",
            input:   User{Email: "test@example.com", Name: "Test"},
            wantErr: false,
        },
        {
            name:    "empty email",
            input:   User{Email: "", Name: "Test"},
            wantErr: true,
        },
        {
            name:    "empty name",
            input:   User{Email: "test@example.com", Name: ""},
            wantErr: true,
        },
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            err := Validate(tt.input)
            if (err != nil) != tt.wantErr {
                t.Errorf("Validate() error = %v, wantErr %v", err, tt.wantErr)
            }
        })
    }
}
```

### Test Helpers

```go
func newTestServer(t *testing.T) *Server {
    t.Helper()  // Marks as helper for better error reporting

    srv := NewServer()
    t.Cleanup(func() {
        srv.Close()
    })
    return srv
}
```

### Mocking with Interfaces

```go
// Mock implementation
type mockRepo struct {
    users map[string]*User
}

func (m *mockRepo) FindByID(ctx context.Context, id string) (*User, error) {
    if user, ok := m.users[id]; ok {
        return user, nil
    }
    return nil, ErrNotFound
}

// In test
func TestUserService_GetUser(t *testing.T) {
    repo := &mockRepo{
        users: map[string]*User{
            "123": {ID: "123", Name: "Test"},
        },
    }
    svc := NewUserService(repo)

    user, err := svc.GetUser(context.Background(), "123")
    if err != nil {
        t.Fatalf("unexpected error: %v", err)
    }
    if user.Name != "Test" {
        t.Errorf("got name %q, want %q", user.Name, "Test")
    }
}
```

### Integration Tests with Build Tags

```go
//go:build integration

package db_test

func TestPostgresRepository(t *testing.T) {
    // Requires running database
    db := setupTestDB(t)
    repo := NewPostgresRepository(db)

    // ... integration tests
}
```

Run with: `go test -tags=integration ./...`

---

## Verification Pipeline

```bash
#!/bin/bash
set -e

echo "=== Go Verification Pipeline ==="

# 1. Format
echo "[1/6] Formatting..."
gofmt -w .
goimports -w .

# 2. Lint
echo "[2/6] Linting..."
go vet ./...
staticcheck ./...
golangci-lint run

# 3. Build
echo "[3/6] Building..."
go build ./...

# 4. Test
echo "[4/6] Testing..."
go test -race -cover ./...

# 5. Security
echo "[5/6] Security scan..."
gosec ./...

# 6. Module check
echo "[6/6] Module verification..."
go mod verify
go mod tidy -diff

echo "=== All checks passed ==="
```

### Makefile

```makefile
.PHONY: build test lint verify

build:
 go build -o bin/myapp ./cmd/myapp

test:
 go test -race -cover ./...

lint:
 golangci-lint run
 go vet ./...

verify: lint test build
 @echo "All checks passed"

run:
 go run ./cmd/myapp

tidy:
 go mod tidy
 go mod verify
```

---

## Quick Reference

| Task | Command |
|------|---------|
| Build | `go build ./...` |
| Test | `go test ./...` |
| Test with race detector | `go test -race ./...` |
| Coverage | `go test -cover ./...` |
| Coverage report | `go test -coverprofile=cover.out ./... && go tool cover -html=cover.out` |
| Lint | `golangci-lint run` |
| Format | `gofmt -w . && goimports -w .` |
| Vet | `go vet ./...` |
| Tidy modules | `go mod tidy` |
| Download deps | `go mod download` |
| Update deps | `go get -u ./...` |
| Security scan | `gosec ./...` |

## Common Gotchas

1. **Loop variable capture**: Always capture loop variables in goroutines
2. **Nil slices vs empty slices**: `var s []int` vs `s := []int{}`
3. **Defer in loops**: Defer runs at function end, not loop iteration
4. **Interface nil check**: Interface can be non-nil but contain nil pointer
5. **Map not safe for concurrent access**: Use `sync.Map` or mutex
