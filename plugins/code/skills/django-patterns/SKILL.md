---
name: django-patterns
description: Production-grade Django architecture and patterns
triggers:
  - Django project setup
  - Django model design
  - DRF API implementation
  - Django ORM optimization
---

# Django Development Patterns

Production architecture for scalable Django applications.

## Project Structure

```
project/
├── config/
│   ├── __init__.py
│   ├── settings/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── development.py
│   │   ├── production.py
│   │   └── test.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── users/
│   │   ├── models.py
│   │   ├── views.py
│   │   ├── serializers.py
│   │   ├── services.py
│   │   └── tests/
│   └── orders/
├── manage.py
└── requirements/
    ├── base.txt
    ├── development.txt
    └── production.txt
```

## Model Design

### Custom User Model

```python
from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    email = models.EmailField(unique=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        indexes = [
            models.Index(fields=['email']),
        ]
```

### Custom QuerySet and Manager

```python
class ProductQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def with_category(self):
        return self.select_related('category')

    def in_stock(self):
        return self.filter(stock__gt=0)

    def for_display(self):
        return self.active().with_category().in_stock()

class Product(models.Model):
    name = models.CharField(max_length=255)
    category = models.ForeignKey('Category', on_delete=models.PROTECT)
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    objects = ProductQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=['is_active', 'stock']),
        ]
```

## ORM Optimization

### N+1 Prevention

```python
# BAD - N+1 queries
for order in Order.objects.all():
    print(order.user.name)  # Query per order

# GOOD - Single query with JOIN
for order in Order.objects.select_related('user'):
    print(order.user.name)

# For many-to-many / reverse FK
orders = Order.objects.prefetch_related('items__product')
```

### Efficient Lookups

```python
# Only fetch needed fields
User.objects.values('id', 'email')
User.objects.only('id', 'email')

# Aggregation
from django.db.models import Count, Avg
Category.objects.annotate(
    product_count=Count('products'),
    avg_price=Avg('products__price')
)

# Bulk operations
Product.objects.bulk_create([
    Product(name='A'), Product(name='B')
])
Product.objects.filter(category_id=1).update(is_active=False)
```

## Django REST Framework

### Serializers

```python
class UserListSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'email', 'username']

class UserDetailSerializer(serializers.ModelSerializer):
    orders_count = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'avatar', 'orders_count']

    def get_orders_count(self, obj):
        return obj.orders.count()

class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ['email', 'username', 'password']

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)
```

### ViewSets

```python
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['email', 'username']
    ordering_fields = ['created_at']

    def get_serializer_class(self):
        if self.action == 'list':
            return UserListSerializer
        if self.action == 'create':
            return UserCreateSerializer
        return UserDetailSerializer

    def get_queryset(self):
        return super().get_queryset().select_related('profile')

    @action(detail=True, methods=['post'])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        user.is_active = False
        user.save()
        return Response({'status': 'deactivated'})
```

## Service Layer

```python
from django.db import transaction
from django.core.exceptions import ValidationError

class OrderService:
    @staticmethod
    @transaction.atomic
    def create_order(user, items):
        """Create order with items atomically."""
        order = Order.objects.create(user=user)

        for item_data in items:
            product = Product.objects.select_for_update().get(
                id=item_data['product_id']
            )

            if product.stock < item_data['quantity']:
                raise ValidationError(f'Insufficient stock for {product.name}')

            product.stock -= item_data['quantity']
            product.save()

            OrderItem.objects.create(
                order=order,
                product=product,
                quantity=item_data['quantity'],
                price=product.price
            )

        return order

    @staticmethod
    def send_confirmation(order):
        """Send order confirmation email."""
        from django.core.mail import send_mail
        send_mail(
            subject=f'Order #{order.id} Confirmed',
            message=f'Your order has been confirmed.',
            from_email='orders@example.com',
            recipient_list=[order.user.email],
        )
```

## Caching

```python
from django.core.cache import cache
from django.views.decorators.cache import cache_page

# View-level caching
@cache_page(60 * 15)  # 15 minutes
def product_list(request):
    ...

# Low-level caching
def get_popular_products():
    cache_key = 'popular_products'
    products = cache.get(cache_key)

    if products is None:
        products = list(
            Product.objects.for_display()
            .annotate(order_count=Count('order_items'))
            .order_by('-order_count')[:10]
        )
        cache.set(cache_key, products, timeout=60*60)

    return products

# Cache invalidation
def on_order_created(order):
    cache.delete('popular_products')
```

## Signals

```python
from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)

@receiver(post_save, sender=Order)
def notify_order_created(sender, instance, created, **kwargs):
    if created:
        from .tasks import send_order_notification
        send_order_notification.delay(instance.id)
```

Register in `apps.py`:

```python
class OrdersConfig(AppConfig):
    name = 'apps.orders'

    def ready(self):
        import apps.orders.signals  # noqa
```

## Settings Pattern

```python
# config/settings/base.py
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

INSTALLED_APPS = [
    'django.contrib.admin',
    # ...
    'rest_framework',
    'apps.users',
    'apps.orders',
]

# config/settings/development.py
from .base import *

DEBUG = True
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'app_dev',
    }
}

# config/settings/production.py
from .base import *

DEBUG = False
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
```

## Testing

```python
from rest_framework.test import APITestCase
from rest_framework import status

class UserAPITest(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='test@example.com',
            password='testpass123'
        )
        self.client.force_authenticate(user=self.user)

    def test_list_users(self):
        response = self.client.get('/api/users/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_create_order(self):
        product = Product.objects.create(name='Test', price=100, stock=10)
        response = self.client.post('/api/orders/', {
            'items': [{'product_id': product.id, 'quantity': 2}]
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        product.refresh_from_db()
        self.assertEqual(product.stock, 8)
```

## Anti-Patterns

| Avoid | Instead |
|-------|---------|
| Logic in views | Service layer |
| `objects.all()` in templates | Paginated QuerySets |
| Signals for everything | Explicit service calls |
| Fat models | Services + thin models |
| Hardcoded settings | Environment variables |
