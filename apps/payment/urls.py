from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import PaymentViewSet, StripeWebhookView

app_name = 'payment'

router = DefaultRouter()
router.register(prefix='', viewset=PaymentViewSet, basename='payment')

urlpatterns = [
    path('webhook/stripe/', StripeWebhookView.as_view(), name='stripe-webhook'),
] + router.urls