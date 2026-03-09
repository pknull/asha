---
description: "Django development patterns, security, TDD, and verification workflows"
triggers:
  - "django"
  - "drf"
  - "django rest framework"
  - "python web"
---

# Django Development Skill

Comprehensive Django and Django REST Framework development guidance covering architecture, security, TDD, and verification.

## Project Structure

```
project/
├── config/
│   ├── settings/
│   │   ├── base.py        # Shared settings
│   │   ├── development.py # Dev overrides
│   │   ├── production.py  # Prod hardening
│   │   └── test.py        # Test settings
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── apps/
│   └── [app_name]/
│       ├── models.py
│       ├── views.py
│       ├── serializers.py
│       ├── urls.py
│       ├── admin.py
│       ├── services.py     # Business logic
│       ├── selectors.py    # Query logic
│       └── tests/
├── requirements/
│   ├── base.txt
│   ├── development.txt
│   └── production.txt
└── manage.py
```

## Architecture Patterns

### Service Layer Pattern

Keep views thin. Business logic in services.

```python
# apps/users/services.py
from django.db import transaction

class UserService:
    @staticmethod
    @transaction.atomic
    def create_user(*, email: str, password: str, **kwargs) -> User:
        user = User.objects.create_user(email=email, password=password, **kwargs)
        send_welcome_email.delay(user.id)
        return user
```

### Selector Pattern

Query logic separate from business logic.

```python
# apps/users/selectors.py
from django.db.models import QuerySet

def get_active_users() -> QuerySet[User]:
    return User.objects.filter(is_active=True).select_related('profile')

def get_user_by_email(email: str) -> User | None:
    return User.objects.filter(email=email).first()
```

### DRF Best Practices

```python
# Serializers: validation and transformation
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'created_at']
        read_only_fields = ['id', 'created_at']

# ViewSets: thin, delegate to services
class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        UserService.create_user(**serializer.validated_data)
```

### ORM Optimization

```python
# Avoid N+1 queries
User.objects.select_related('profile')        # ForeignKey, OneToOne
User.objects.prefetch_related('posts')        # ManyToMany, reverse FK

# Use .only() and .defer() for large models
User.objects.only('id', 'email')

# Bulk operations
User.objects.bulk_create([...])
User.objects.bulk_update([...], fields=['status'])
```

---

## Security Configuration

### Production Settings

```python
# config/settings/production.py
DEBUG = False
ALLOWED_HOSTS = ['yourdomain.com']

# HTTPS
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# HSTS
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Cookies
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Strict'

# Security headers
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
```

### Authentication

```python
# Custom user model (do this first!)
AUTH_USER_MODEL = 'users.User'

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# DRF authentication
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
    },
}
```

### Input Validation

```python
# Always validate file uploads
def validate_file(file):
    allowed_types = ['image/jpeg', 'image/png', 'application/pdf']
    max_size = 10 * 1024 * 1024  # 10MB

    if file.content_type not in allowed_types:
        raise ValidationError('Invalid file type')
    if file.size > max_size:
        raise ValidationError('File too large')
```

---

## Test-Driven Development

### Test Configuration

```ini
# pytest.ini
[pytest]
DJANGO_SETTINGS_MODULE = config.settings.test
python_files = test_*.py
addopts = --reuse-db --nomigrations -v --cov=apps --cov-report=term-missing
```

```python
# config/settings/test.py
from .base import *

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
```

### Factory Pattern

```python
# apps/users/tests/factories.py
import factory
from apps.users.models import User

class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f'user{n}@example.com')
    is_active = True

    @factory.post_generation
    def password(self, create, extracted, **kwargs):
        self.set_password(extracted or 'testpass123')
```

### Test Examples

```python
# apps/users/tests/test_services.py
import pytest
from apps.users.services import UserService
from apps.users.tests.factories import UserFactory

@pytest.mark.django_db
class TestUserService:
    def test_create_user_success(self):
        # RED: Write failing test
        user = UserService.create_user(
            email='test@example.com',
            password='securepass123'
        )

        # GREEN: Assert expected behavior
        assert user.email == 'test@example.com'
        assert user.check_password('securepass123')
        assert user.is_active

    def test_create_user_duplicate_email_fails(self):
        UserFactory(email='existing@example.com')

        with pytest.raises(IntegrityError):
            UserService.create_user(
                email='existing@example.com',
                password='password123'
            )
```

```python
# apps/users/tests/test_api.py
import pytest
from rest_framework.test import APIClient
from rest_framework import status

@pytest.mark.django_db
class TestUserAPI:
    def setup_method(self):
        self.client = APIClient()
        self.user = UserFactory()
        self.client.force_authenticate(user=self.user)

    def test_list_users(self):
        response = self.client.get('/api/users/')
        assert response.status_code == status.HTTP_200_OK

    def test_unauthenticated_access_denied(self):
        self.client.force_authenticate(user=None)
        response = self.client.get('/api/users/')
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
```

---

## Verification Pipeline

Run before PR or deployment:

```bash
#!/bin/bash
set -e

echo "=== Django Verification Pipeline ==="

# 1. Environment
echo "[1/6] Checking environment..."
python --version
python -c "import django; print(f'Django {django.VERSION}')"

# 2. Code Quality
echo "[2/6] Running linters..."
ruff check apps/
black --check apps/
isort --check-only apps/

# 3. Type Checking
echo "[3/6] Type checking..."
mypy apps/ --ignore-missing-imports

# 4. Django Checks
echo "[4/6] Django system checks..."
python manage.py check --deploy
python manage.py makemigrations --check --dry-run

# 5. Tests
echo "[5/6] Running tests..."
pytest --cov=apps --cov-fail-under=80

# 6. Security
echo "[6/6] Security scanning..."
bandit -r apps/ -ll
safety check
pip-audit

echo "=== All checks passed ==="
```

### Deploy Checklist

- [ ] `DEBUG = False`
- [ ] `SECRET_KEY` from environment
- [ ] `ALLOWED_HOSTS` configured
- [ ] HTTPS enforced
- [ ] Database is PostgreSQL (not SQLite)
- [ ] Static files collected
- [ ] Migrations applied
- [ ] Logging configured
- [ ] Error monitoring (Sentry) connected

---

## Quick Reference

| Task | Command |
|------|---------|
| New app | `python manage.py startapp app_name apps/app_name` |
| Migrations | `python manage.py makemigrations && python manage.py migrate` |
| Shell | `python manage.py shell_plus` (django-extensions) |
| Test | `pytest apps/` |
| Coverage | `pytest --cov=apps --cov-report=html` |
| Lint | `ruff check apps/ && black apps/` |
| Security | `bandit -r apps/ && safety check` |
