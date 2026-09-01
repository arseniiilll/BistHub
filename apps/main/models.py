from django.db import models, IntegrityError, transaction
from django.utils.text import slugify
from django.core.validators import MinValueValidator, MaxValueValidator
from django.conf import settings


DEFAULT_HEALTH_WARNING = (
    "Acest produs conține nicotină, care creează dependență. "
    "Fumatul dăunează grav sănătății dumneavoastră și celor din jur."
)


class Tobacco(models.Model):

    class Strength(models.TextChoices):
        LIGHT = 'light', 'Light'
        HARD = 'hard', 'Hard'

    name = models.CharField(max_length=255, verbose_name='Name')
    # allow_unicode=True — обязательно, иначе валидатор поля не совпадает с тем,
    # что реально генерирует _generate_unique_slug() через slugify(..., allow_unicode=True)
    slug = models.SlugField(
        max_length=255, unique=True, blank=True, allow_unicode=True, verbose_name='Slug'
    )
    description = models.TextField(blank=True, verbose_name='Description')
    flavor = models.CharField(max_length=255, verbose_name='Flavor')
    strength = models.CharField(
        max_length=10,
        choices=Strength.choices,
        verbose_name='Strength'
    )
    photo = models.ImageField(upload_to='tobacco/photos/', blank=True, null=True, verbose_name='Photo')
    price = models.DecimalField(max_digits=8, decimal_places=2, verbose_name='Price')
    stock_quantity = models.PositiveIntegerField(default=0, verbose_name='Stock quantity')
    is_available = models.BooleanField(default=True, verbose_name='Is available')

    # --- Compliance / EU Tobacco Products Directive ---
    requires_age_verification = models.BooleanField(
        default=True,
        verbose_name='Requires age verification',
        help_text='Should almost always be True for tobacco products sold in the EU.'
    )
    health_warning_text = models.TextField(
        verbose_name='Health warning text',
        help_text='Mandatory health warning to display on the product page, per EU TPD (2014/40/EU).',
        default=DEFAULT_HEALTH_WARNING,
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Created at')

    class Meta:
        verbose_name = 'Tobacco'
        verbose_name_plural = 'Tobaccos'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.get_strength_display()})'

    def save(self, *args, **kwargs):
        # auto_slug=True — значит slug не задан явно, а сгенерирован нами, и
        # его можно безопасно перегенерировать при коллизии. Если slug пришёл
        # готовым (например, из фикстуры/скрипта), ретраить его нельзя —
        # тогда IntegrityError должен долететь до вызывающего кода как есть.
        auto_slug = not self.slug
        if auto_slug:
            self.slug = self._generate_unique_slug()

        # _generate_unique_slug() проверяет уникальность запросом ДО insert —
        # между проверкой и записью два параллельных save() с одинаковым name
        # могут одновременно увидеть один и тот же свободный slug. Уникальность
        # в БД (unique=True) всё равно не даст создать дубль, но без retry это
        # был бы необработанный IntegrityError вместо тихого решения проблемы.
        attempts = 0
        while True:
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError:
                attempts += 1
                if not auto_slug or attempts >= 5:
                    raise
                self.slug = self._generate_unique_slug()

    def _generate_unique_slug(self):
        """slugify(name) с числовым суффиксом при коллизии: два товара с
        одинаковым name (например, одна и та же позиция из разных партий)
        иначе дают одинаковый slug и падают в IntegrityError на unique=True."""
        base_slug = slugify(self.name, allow_unicode=True)
        slug = base_slug
        suffix = 2
        qs = Tobacco.objects.filter(slug=slug)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        while qs.exists():
            slug = f'{base_slug}-{suffix}'
            suffix += 1
            qs = Tobacco.objects.filter(slug=slug)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
        return slug


class ProductImage(models.Model):
    """Галерея изображений товара. product.photo остаётся как главное/обложечное фото."""
    tobacco = models.ForeignKey(
        Tobacco,
        on_delete=models.CASCADE,
        related_name='images',
        verbose_name='Tobacco'
    )
    image = models.ImageField(upload_to='tobacco/gallery/', verbose_name='Image')
    is_main = models.BooleanField(default=False, verbose_name='Is main image')
    order = models.PositiveIntegerField(default=0, verbose_name='Display order')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Created at')

    class Meta:
        verbose_name = 'Product image'
        verbose_name_plural = 'Product images'
        ordering = ['order', 'created_at']

    def __str__(self):
        return f'Image for {self.tobacco.name} (#{self.pk})'

    def save(self, *args, **kwargs):
        # Оборачиваем save() + сброс is_main у остальных картинок в одну
        # транзакцию: иначе между двумя запросами есть окно, где для одного
        # tobacco временно могут читаться две картинки с is_main=True.
        with transaction.atomic():
            super().save(*args, **kwargs)
            if self.is_main:
                ProductImage.objects.filter(tobacco=self.tobacco).exclude(pk=self.pk).update(is_main=False)


class Review(models.Model):
    tobacco = models.ForeignKey(
        Tobacco,
        on_delete=models.CASCADE,
        related_name='reviews',
        verbose_name='Tobacco'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reviews',
        verbose_name='Author'
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name='Rating'
    )
    text = models.TextField(blank=True, verbose_name='Review text')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Created at')

    class Meta:
        verbose_name = 'Review'
        verbose_name_plural = 'Reviews'
        ordering = ['-created_at']
        unique_together = ('tobacco', 'author')

    def __str__(self):
        return f'{self.author} — {self.tobacco.name} ({self.rating}★)'