from django.db import IntegrityError
from rest_framework import serializers
from .models import Tobacco, ProductImage, Review
from apps.main.customfield import PriceField

class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'is_main', 'order']


class ReviewSerializer(serializers.ModelSerializer):
    """Для чтения — автор отдаётся коротким представлением, без лишних личных данных."""
    author_name = serializers.CharField(source='author.full_name', read_only=True)

    class Meta:
        model = Review
        fields = ['id', 'tobacco', 'author', 'author_name', 'rating', 'text', 'created_at']
        read_only_fields = ['id', 'tobacco', 'author', 'author_name', 'created_at']


class ReviewCreateSerializer(serializers.ModelSerializer):
    """Автор проставляется из request.user, не принимается с клиента."""
    # Без явного queryset PrimaryKeyRelatedField по умолчанию использует
    # Tobacco.objects.all() — это позволило бы оставить отзыв на товар,
    # скрытый/снятый с продажи и невидимый в публичном каталоге.
    tobacco = serializers.PrimaryKeyRelatedField(queryset=Tobacco.objects.filter(is_available=True))

    class Meta:
        model = Review
        fields = ['tobacco', 'rating', 'text']

    def validate(self, attrs):
        request = self.context['request']
        if Review.objects.filter(tobacco=attrs['tobacco'], author=request.user).exists():
            raise serializers.ValidationError('Вы уже оставляли отзыв на этот товар.')
        return attrs

    def create(self, validated_data):
        validated_data['author'] = self.context['request'].user
        # validate() уже проверяет дубль, но это не защита от гонки: два
        # параллельных запроса от одного автора на один товар оба могут
        # пройти validate() до того, как любой из них сохранится. unique_together
        # на модели тогда защитит данные, но без этого except второй запрос
        # уронит необработанный IntegrityError (500) вместо аккуратного 400.
        try:
            return super().create(validated_data)
        except IntegrityError:
            raise serializers.ValidationError('Вы уже оставляли отзыв на этот товар.')


class TobaccoListSerializer(serializers.ModelSerializer):
    """Облегчённая версия для списков/каталога — без reviews и полного description."""
    main_image = serializers.SerializerMethodField()
    price = PriceField()
    class Meta:
        model = Tobacco
        fields = ['id', 'name', 'slug', 'flavor', 'strength', 'price', 'is_available', 'main_image']

    def get_main_image(self, obj):
        # obj.images.filter(...) всегда бьёт в БД заново, игнорируя
        # prefetch_related('images') из TobaccoViewSet.queryset — на списке
        # из N товаров это N лишних запросов. obj.images.all() использует
        # уже загруженный prefetch-кэш, дальше фильтруем в Python.
        images = list(obj.images.all())
        if not images:
            return None
        main = next((img for img in images if img.is_main), images[0])
        request = self.context.get('request')
        return request.build_absolute_uri(main.image.url) if request else main.image.url


class TobaccoDetailSerializer(serializers.ModelSerializer):
    """Полная карточка товара — для страницы товара."""
    images = ProductImageSerializer(many=True, read_only=True)
    reviews = ReviewSerializer(many=True, read_only=True)
    average_rating = serializers.SerializerMethodField()
    price = PriceField()
    class Meta:
        model = Tobacco
        fields = [
            'id', 'name', 'slug', 'description', 'flavor', 'strength',
            'photo', 'price', 'stock_quantity', 'is_available',
            'requires_age_verification', 'health_warning_text',
            'images', 'reviews', 'average_rating', 'created_at',
        ]

    def get_average_rating(self, obj):
        ratings = [r.rating for r in obj.reviews.all()]
        return round(sum(ratings) / len(ratings), 1) if ratings else None