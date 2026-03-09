---
description: "Spring Boot development patterns, security, TDD, and verification workflows"
triggers:
  - "spring boot"
  - "spring"
  - "java api"
  - "kotlin spring"
---

# Spring Boot Development Skill

Comprehensive Spring Boot development guidance covering architecture, security, TDD, and verification.

## Project Structure

```
src/
├── main/
│   ├── java/com/example/app/
│   │   ├── config/           # Configuration classes
│   │   ├── controller/       # REST controllers
│   │   ├── service/          # Business logic
│   │   ├── repository/       # Data access
│   │   ├── model/            # Entities and DTOs
│   │   ├── exception/        # Custom exceptions
│   │   └── Application.java
│   └── resources/
│       ├── application.yml
│       ├── application-dev.yml
│       ├── application-prod.yml
│       └── db/migration/     # Flyway migrations
└── test/
    └── java/com/example/app/
        ├── controller/
        ├── service/
        └── repository/
```

## Architecture Patterns

### Layered Architecture

```java
// Controller: thin, handles HTTP only
@RestController
@RequestMapping("/api/users")
@RequiredArgsConstructor
public class UserController {
    private final UserService userService;

    @GetMapping("/{id}")
    public ResponseEntity<UserDto> getUser(@PathVariable Long id) {
        return ResponseEntity.ok(userService.findById(id));
    }

    @PostMapping
    public ResponseEntity<UserDto> createUser(@Valid @RequestBody CreateUserRequest request) {
        UserDto user = userService.create(request);
        return ResponseEntity.status(HttpStatus.CREATED).body(user);
    }
}
```

```java
// Service: business logic, transactions
@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class UserService {
    private final UserRepository userRepository;
    private final PasswordEncoder passwordEncoder;

    public UserDto findById(Long id) {
        return userRepository.findById(id)
            .map(UserDto::from)
            .orElseThrow(() -> new UserNotFoundException(id));
    }

    @Transactional
    public UserDto create(CreateUserRequest request) {
        if (userRepository.existsByEmail(request.email())) {
            throw new EmailAlreadyExistsException(request.email());
        }
        User user = User.builder()
            .email(request.email())
            .password(passwordEncoder.encode(request.password()))
            .build();
        return UserDto.from(userRepository.save(user));
    }
}
```

```java
// Repository: data access only
@Repository
public interface UserRepository extends JpaRepository<User, Long> {
    Optional<User> findByEmail(String email);
    boolean existsByEmail(String email);

    @Query("SELECT u FROM User u WHERE u.status = :status")
    List<User> findByStatus(@Param("status") UserStatus status);
}
```

### DTOs and Validation

```java
// Request DTO with validation
public record CreateUserRequest(
    @NotBlank @Email String email,
    @NotBlank @Size(min = 12) String password,
    @NotBlank String name
) {}

// Response DTO
public record UserDto(
    Long id,
    String email,
    String name,
    Instant createdAt
) {
    public static UserDto from(User user) {
        return new UserDto(user.getId(), user.getEmail(),
                          user.getName(), user.getCreatedAt());
    }
}
```

### Global Exception Handling

```java
@RestControllerAdvice
public class GlobalExceptionHandler extends ResponseEntityExceptionHandler {

    @ExceptionHandler(UserNotFoundException.class)
    public ResponseEntity<ProblemDetail> handleUserNotFound(UserNotFoundException ex) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(
            HttpStatus.NOT_FOUND, ex.getMessage());
        problem.setTitle("User Not Found");
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(problem);
    }

    @ExceptionHandler(ConstraintViolationException.class)
    public ResponseEntity<ProblemDetail> handleValidation(ConstraintViolationException ex) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(
            HttpStatus.BAD_REQUEST, "Validation failed");
        problem.setProperty("violations", ex.getConstraintViolations());
        return ResponseEntity.badRequest().body(problem);
    }
}
```

### JPA Optimization

```java
// Avoid N+1 with fetch joins
@Query("SELECT u FROM User u JOIN FETCH u.roles WHERE u.id = :id")
Optional<User> findByIdWithRoles(@Param("id") Long id);

// Projections for read-only queries
public interface UserSummary {
    Long getId();
    String getEmail();
}

@Query("SELECT u.id as id, u.email as email FROM User u")
List<UserSummary> findAllSummaries();

// Pagination
Page<User> findByStatus(UserStatus status, Pageable pageable);
```

---

## Security Configuration

### Spring Security Setup

```java
@Configuration
@EnableWebSecurity
@EnableMethodSecurity
public class SecurityConfig {

    @Bean
    public SecurityFilterChain filterChain(HttpSecurity http) throws Exception {
        return http
            .csrf(csrf -> csrf.disable()) // Disable for stateless API
            .sessionManagement(session ->
                session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .authorizeHttpRequests(auth -> auth
                .requestMatchers("/api/auth/**").permitAll()
                .requestMatchers("/actuator/health").permitAll()
                .anyRequest().authenticated())
            .oauth2ResourceServer(oauth2 -> oauth2.jwt(Customizer.withDefaults()))
            .build();
    }

    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder(12);
    }
}
```

### Method Security

```java
@Service
@RequiredArgsConstructor
public class DocumentService {

    @PreAuthorize("hasRole('ADMIN')")
    public void deleteAll() { /* ... */ }

    @PreAuthorize("hasRole('USER') and @authz.canAccess(#id)")
    public Document findById(Long id) { /* ... */ }

    @PostAuthorize("returnObject.owner == authentication.name")
    public Document create(CreateDocumentRequest request) { /* ... */ }
}

// Custom authorization component
@Component("authz")
public class AuthorizationService {
    public boolean canAccess(Long resourceId) {
        // Custom authorization logic
        return true;
    }
}
```

### Production Properties

```yaml
# application-prod.yml
server:
  ssl:
    enabled: true
  servlet:
    session:
      cookie:
        secure: true
        http-only: true
        same-site: strict

spring:
  datasource:
    url: ${DATABASE_URL}
    username: ${DATABASE_USER}
    password: ${DATABASE_PASSWORD}
    hikari:
      maximum-pool-size: 10
      minimum-idle: 5

management:
  endpoints:
    web:
      exposure:
        include: health,metrics,prometheus
  endpoint:
    health:
      show-details: never
```

---

## Test-Driven Development

### Test Configuration

```java
// Test slice for repository layer
@DataJpaTest
@AutoConfigureTestDatabase(replace = Replace.NONE)
@Testcontainers
class UserRepositoryTest {

    @Container
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:15");

    @DynamicPropertySource
    static void configureProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", postgres::getJdbcUrl);
        registry.add("spring.datasource.username", postgres::getUsername);
        registry.add("spring.datasource.password", postgres::getPassword);
    }

    @Autowired
    private UserRepository userRepository;

    @Test
    void shouldFindUserByEmail() {
        // Given
        User user = User.builder().email("test@example.com").build();
        userRepository.save(user);

        // When
        Optional<User> found = userRepository.findByEmail("test@example.com");

        // Then
        assertThat(found).isPresent();
        assertThat(found.get().getEmail()).isEqualTo("test@example.com");
    }
}
```

```java
// Test slice for web layer
@WebMvcTest(UserController.class)
class UserControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private UserService userService;

    @Test
    @WithMockUser
    void shouldReturnUser() throws Exception {
        // Given
        UserDto user = new UserDto(1L, "test@example.com", "Test", Instant.now());
        when(userService.findById(1L)).thenReturn(user);

        // When & Then
        mockMvc.perform(get("/api/users/1"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.email").value("test@example.com"));
    }

    @Test
    @WithMockUser
    void shouldReturn404WhenUserNotFound() throws Exception {
        when(userService.findById(999L)).thenThrow(new UserNotFoundException(999L));

        mockMvc.perform(get("/api/users/999"))
            .andExpect(status().isNotFound());
    }
}
```

```java
// Service layer unit test
@ExtendWith(MockitoExtension.class)
class UserServiceTest {

    @Mock
    private UserRepository userRepository;

    @Mock
    private PasswordEncoder passwordEncoder;

    @InjectMocks
    private UserService userService;

    @Test
    void shouldCreateUser() {
        // Given
        CreateUserRequest request = new CreateUserRequest(
            "test@example.com", "password123", "Test User");
        when(userRepository.existsByEmail(any())).thenReturn(false);
        when(passwordEncoder.encode(any())).thenReturn("encoded");
        when(userRepository.save(any())).thenAnswer(inv -> {
            User u = inv.getArgument(0);
            u.setId(1L);
            return u;
        });

        // When
        UserDto result = userService.create(request);

        // Then
        assertThat(result.email()).isEqualTo("test@example.com");
        verify(userRepository).save(any());
    }
}
```

### Integration Test

```java
@SpringBootTest(webEnvironment = WebEnvironment.RANDOM_PORT)
@Testcontainers
class UserIntegrationTest {

    @Container
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:15");

    @Autowired
    private TestRestTemplate restTemplate;

    @Test
    void shouldCreateAndRetrieveUser() {
        // Create
        CreateUserRequest request = new CreateUserRequest(
            "integration@test.com", "password123456", "Integration Test");
        ResponseEntity<UserDto> createResponse = restTemplate.postForEntity(
            "/api/users", request, UserDto.class);

        assertThat(createResponse.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        Long userId = createResponse.getBody().id();

        // Retrieve
        ResponseEntity<UserDto> getResponse = restTemplate.getForEntity(
            "/api/users/" + userId, UserDto.class);

        assertThat(getResponse.getStatusCode()).isEqualTo(HttpStatus.OK);
        assertThat(getResponse.getBody().email()).isEqualTo("integration@test.com");
    }
}
```

---

## Verification Pipeline

```bash
#!/bin/bash
set -e

echo "=== Spring Boot Verification Pipeline ==="

# 1. Build
echo "[1/6] Building project..."
./mvnw clean verify -DskipTests

# 2. Static Analysis
echo "[2/6] Static analysis..."
./mvnw spotbugs:check pmd:check checkstyle:check

# 3. Tests
echo "[3/6] Running tests..."
./mvnw test

# 4. Coverage
echo "[4/6] Checking coverage..."
./mvnw jacoco:report jacoco:check

# 5. Security
echo "[5/6] Security scanning..."
./mvnw org.owasp:dependency-check-maven:check
gitleaks detect --source . --verbose

# 6. Integration Tests
echo "[6/6] Integration tests..."
./mvnw verify -Pintegration-tests

echo "=== All checks passed ==="
```

### Maven Configuration

```xml
<!-- pom.xml plugins -->
<plugin>
    <groupId>org.jacoco</groupId>
    <artifactId>jacoco-maven-plugin</artifactId>
    <configuration>
        <rules>
            <rule>
                <limits>
                    <limit>
                        <counter>LINE</counter>
                        <minimum>0.80</minimum>
                    </limit>
                </limits>
            </rule>
        </rules>
    </configuration>
</plugin>

<plugin>
    <groupId>org.owasp</groupId>
    <artifactId>dependency-check-maven</artifactId>
    <configuration>
        <failBuildOnCVSS>7</failBuildOnCVSS>
    </configuration>
</plugin>
```

### Deploy Checklist

- [ ] Profile set to `prod`
- [ ] Database credentials from environment/secrets
- [ ] SSL/TLS configured
- [ ] Actuator endpoints secured
- [ ] Health checks responding
- [ ] Flyway migrations applied
- [ ] Logging to external system
- [ ] Metrics exposed to Prometheus
- [ ] Error tracking (Sentry) connected

---

## Quick Reference

| Task | Command |
|------|---------|
| Run | `./mvnw spring-boot:run` |
| Test | `./mvnw test` |
| Coverage | `./mvnw jacoco:report` |
| Package | `./mvnw package -DskipTests` |
| Security scan | `./mvnw dependency-check:check` |
| Format | `./mvnw spotless:apply` |
| Build image | `./mvnw spring-boot:build-image` |
