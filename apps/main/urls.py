from rest_framework.routers import DefaultRouter
from .views import TobaccoViewSet, ReviewViewSet

app_name = 'main'

router = DefaultRouter()
router.register('shop/tobacco', TobaccoViewSet, basename='tobacco')
router.register('reviews', ReviewViewSet, basename='review')

urlpatterns = router.urls