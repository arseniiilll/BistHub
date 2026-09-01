from rest_framework import viewsets, permissions, filters
from django_filters.rest_framework import DjangoFilterBackend

from .models import Tobacco, Review
from .serializers import (
    TobaccoListSerializer, TobaccoDetailSerializer,
    ReviewSerializer, ReviewCreateSerializer,
)
from apps.permissions import IsOwner


class TobaccoViewSet(viewsets.ReadOnlyModelViewSet):
    """Каталог товаров — только чтение, изменения только через админку.
    /api/tobacco/ — список, /api/tobacco/{slug}/ — карточка товара."""
    permission_classes = [permissions.AllowAny]
    lookup_field = 'slug'

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['strength']
    search_fields = ['name', 'flavor', 'description']
    ordering_fields = ['price', 'created_at']
    ordering = ['-created_at']

    def get_queryset(self):
        # TobaccoListSerializer использует только 'images' (main_image) и
        # никогда не трогает reviews — раньше 'reviews__author' грузился
        # prefetch'ем на КАЖДЫЙ list-запрос впустую. reviews нужны только
        # для карточки товара (TobaccoDetailSerializer.reviews).
        qs = Tobacco.objects.filter(is_available=True).prefetch_related('images')
        if self.action == 'retrieve':
            qs = qs.prefetch_related('reviews__author')
        return qs

    def get_serializer_class(self):
        return TobaccoListSerializer if self.action == 'list' else TobaccoDetailSerializer


class ReviewViewSet(viewsets.ModelViewSet):
    """Отзывы. Список — публичный, создание — только авторизованным,
    редактирование/удаление — только автору отзыва."""
    queryset = Review.objects.select_related('author', 'tobacco')
    http_method_names = ['get', 'post', 'patch', 'delete']
    owner_field = 'author'  # используется в IsOwner

    # ?tobacco=<id> — фильтрация через DjangoFilterBackend вместо ручного
    # query_params.get + filter(tobacco_id=...): при нечисловом значении
    # (?tobacco=abc) backend аккуратно вернёт пустой queryset вместо
    # ValueError/500 из БД-драйвера при приведении к int.
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tobacco']

    def get_permissions(self):
        if self.action == 'create':
            return [permissions.IsAuthenticated()]
        if self.action in ('update', 'partial_update', 'destroy'):
            return [permissions.IsAuthenticated(), IsOwner()]
        return [permissions.AllowAny()]

    def get_serializer_class(self):
        return ReviewCreateSerializer if self.action == 'create' else ReviewSerializer