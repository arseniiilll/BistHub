from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import CartView, CartItemViewSet

app_name = 'cart'

router = DefaultRouter()
router.register('cart-items', CartItemViewSet, basename='cart-item')

urlpatterns = [
    path('cart/', CartView.as_view(), name='cart'),
] + router.urls