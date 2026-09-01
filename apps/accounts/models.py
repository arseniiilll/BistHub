from django.db import models
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


def calculate_age(date_of_birth, today=None):
    """Общая точка правды для подсчёта возраста — используется и в User.age,
    и в валидаторах даты рождения в serializers.py. Раньше serializers.py
    считал через date.today() (наивная серверная дата), а модель — через
    timezone.now().date() (дата в TIME_ZONE проекта). Если сервер и
    TIME_ZONE в разных часовых поясах, у полуночи эти два способа могли
    вернуть разный календарный день и, соответственно, разный возраст."""
    if today is None:
        today = timezone.now().date()
    return today.year - date_of_birth.year - (
        (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
    )


class UserManager(BaseUserManager):
    """Стоковый UserManager из AbstractUser рассчитан на
    create_user(username, email=None, password=None, ...) — первым
    позиционным аргументом идёт username. У нас USERNAME_FIELD='email',
    и если где-то (сиды, тесты, shell) вызовут
    User.objects.create_user(email, password) позиционно, значения
    перепутаются местами. Задаём сигнатуру явно, email — первым."""
    use_in_migrations = True

    def _create_user(self, email, username, password, **extra_fields):
        if not email:
            raise ValueError('Email обязателен.')
        if not username:
            raise ValueError('Username обязателен.')
        email = self.normalize_email(email).lower()
        user = self.model(email=email, username=username, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email=None, username=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, username, password, **extra_fields)

    def create_superuser(self, email=None, username=None, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Суперпользователь должен иметь is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Суперпользователь должен иметь is_superuser=True.')
        return self._create_user(email, username, password, **extra_fields)


class User(AbstractUser):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=255, blank=True)
    last_name = models.CharField(max_length=255, blank=True)
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    bio = models.TextField(max_length=500, blank=True)

    # Нужно для проверки возраста при продаже табака (EU TPD / Law 201/2016).
    # null=True на уровне БД, чтобы миграция не ломалась на уже существующих
    # пользователях без даты рождения — но для новых регистраций поле
    # обязательно на уровне сериализатора (см. UserRegistrationSerializer).
    date_of_birth = models.DateField(null=True, blank=True, verbose_name='Date of birth')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    objects = UserManager()

    class Meta:
        db_table = 'users'
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        # email__iexact в UserRegistrationSerializer.validate_email() ловит
        # дубликаты по регистру на входе, но без нормализации самой записи
        # "User@x.com" и "user@x.com" остаются РАЗНЫМИ строками в БД —
        # unique=True на email их не поймает при обходе сериализатора
        # (админка, shell, фикстуры), а authenticate() в LoginView делает
        # точное сравнение по USERNAME_FIELD, так что залогиниться под
        # другим регистром может не получиться. Приводим email к нижнему
        # регистру при каждом save(), чтобы unique-constraint в БД стал
        # настоящей регистронезависимой защитой, а не только на бумаге.
        if self.email:
            self.email = self.email.lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'.strip()

    @property
    def age(self):
        """Возраст в полных годах либо None, если дата рождения не указана."""
        if not self.date_of_birth:
            return None
        return calculate_age(self.date_of_birth)

    @property
    def is_of_legal_age(self):
        """False и для несовершеннолетних, и для пользователей без указанной
        даты рождения — отсутствие данных не считается подтверждением возраста."""
        age = self.age
        return age is not None and age >= 18