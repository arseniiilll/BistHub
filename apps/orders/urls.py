from rest_framework.routers import DefaultRouter
from .views import OrderViewSet

app_name = 'orders'

router = DefaultRouter()
# Префикс 'orders' здесь не указываем: он уже задаётся в корневом urls.py
# проекта при подключении этого модуля (path('orders/', include(...))).
# Если добавить его и тут, итоговые пути превратятся в /orders/orders/.
router.register('orders', OrderViewSet, basename='order')

urlpatterns = router.urls