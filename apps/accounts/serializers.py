from rest_framework import serializers
from django.contrib.auth import get_user_model, authenticate
from django.contrib.auth.password_validation import validate_password as django_validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.utils import timezone

from .models import calculate_age

User = get_user_model()


def _validate_legal_age(value, error_message):
    """Общий валидатор даты рождения для регистрации и обновления профиля.
    today берётся из timezone.now().date(), той же функцией calculate_age(),
    что использует User.age — иначе модель и сериализаторы могут разойтись
    в подсчёте возраста возле полуночи, если TIME_ZONE не совпадает с
    часовым поясом сервера."""
    today = timezone.now().date()
    if value > today:
        raise serializers.ValidationError('Некорректная дата рождения.')
    if calculate_age(value, today) < 18:
        raise serializers.ValidationError(error_message)
    return value


class UserSerializer(serializers.ModelSerializer):
    """Профиль пользователя — то, что отдаётся клиенту после логина/регистрации."""
    full_name = serializers.CharField(read_only=True)

    class Meta:
        model = User
        fields = ['id', 'email', 'username', 'first_name', 'last_name', 'full_name',
                  'avatar', 'bio', 'date_of_birth', 'created_at']
        read_only_fields = ['id', 'email', 'created_at']


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    password2 = serializers.CharField(write_only=True, min_length=8)
    # Обязательно при регистрации: без даты рождения нельзя подтвердить 18+,
    # а без подтверждённого возраста продавать табак нельзя.
    date_of_birth = serializers.DateField(required=True)

    class Meta:
        model = User
        fields = ['email', 'username', 'first_name', 'last_name', 'date_of_birth', 'password', 'password2']

    def validate_email(self, value):
        # Нормализуем регистр здесь же, до проверки на дубликат — само
        # хранение в нижнем регистре обеспечивает User.save().
        value = value.lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError('Пользователь с таким email уже существует.')
        return value

    def validate_username(self, value):
        # username тоже unique=True (унаследовано от AbstractUser), но до этой
        # правки проверялся только email — попытка занятого username долетала
        # необработанной до IntegrityError в create() и превращалась в 500.
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('Пользователь с таким username уже существует.')
        return value

    def validate_date_of_birth(self, value):
        return _validate_legal_age(value, 'Регистрация доступна только пользователям 18 лет и старше.')

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password2'):
            raise serializers.ValidationError({'password2': 'Пароли не совпадают.'})

        # Django-валидаторы сложности пароля (длина/схожесть с юзернеймом-email/
        # распространённость/только цифры — см. AUTH_PASSWORD_VALIDATORS в settings.py).
        # Передаём временного User, чтобы UserAttributeSimilarityValidator мог
        # сверить пароль с email/username/именем, а не только с самим собой.
        temp_user = User(
            email=attrs.get('email', ''),
            username=attrs.get('username', ''),
            first_name=attrs.get('first_name', ''),
            last_name=attrs.get('last_name', ''),
        )
        try:
            django_validate_password(attrs['password'], user=temp_user)
        except DjangoValidationError as e:
            raise serializers.ValidationError({'password': list(e.messages)})

        return attrs

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = User(**validated_data)
        user.set_password(password)
        try:
            user.save()
        except IntegrityError:
            # validate_email()/validate_username() уже проверяли уникальность,
            # но между этой проверкой и save() параллельный запрос мог занять
            # тот же email ИЛИ тот же username первым — оба поля unique=True.
            # Перепроверяем оба, чтобы отдать ошибку на то поле, которое
            # реально столкнулось, а не наугад приписывать её email.
            errors = {}
            if User.objects.filter(email__iexact=user.email).exists():
                errors['email'] = ['Пользователь с таким email уже существует.']
            if User.objects.filter(username=user.username).exists():
                errors['username'] = ['Пользователь с таким username уже существует.']
            if not errors:
                # Ни одно из проверяемых полей не оказалось занято — значит,
                # IntegrityError вызван чем-то другим (например, констрейнтом
                # на другом поле). Не выдумываем причину, отдаём общую ошибку.
                errors['non_field_errors'] = ['Не удалось создать пользователя. Попробуйте ещё раз.']
            raise serializers.ValidationError(errors)
        return user


class UserLoginSerializer(serializers.Serializer):
    """Не ModelSerializer — тут нет сохраняемого объекта, только валидация связки email+password."""
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        email = attrs['email'].lower()
        password = attrs['password']

        # ModelBackend.authenticate() сам отфильтровывает is_active=False
        # ДО проверки пароля и просто возвращает None — так что проверка
        # "if not user.is_active" после authenticate() никогда не выполнится,
        # и деактивированный пользователь всегда получал бы обычное
        # "Неверный email или пароль" вместо "Аккаунт деактивирован".
        # Поэтому сверяем email+пароль вручную, чтобы точно узнать причину
        # отказа, прежде чем звать authenticate().
        try:
            candidate = User.objects.get(email=email)
        except User.DoesNotExist:
            candidate = None

        if candidate is not None and not candidate.check_password(password):
            candidate = None  # неверный пароль — не отличаем от "нет такого email"

        if candidate is None:
            raise serializers.ValidationError('Неверный email или пароль.')
        if not candidate.is_active:
            raise serializers.ValidationError('Аккаунт деактивирован.')

        # USERNAME_FIELD='email' у нашей модели, поэтому authenticate ищет по
        # username=email. .lower() — чтобы совпадать с тем, как email хранится
        # в БД после нормализации в User.save(). Всё ещё зовём authenticate()
        # (а не просто возвращаем candidate), чтобы отработали все настроенные
        # AUTHENTICATION_BACKENDS (не только пароль — например, блокировки
        # по числу попыток, если такой бэкенд подключён).
        user = authenticate(username=email, password=password)
        if user is None:
            raise serializers.ValidationError('Неверный email или пароль.')

        attrs['user'] = user
        return attrs


class UserUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'avatar', 'bio', 'date_of_birth']

    def validate_date_of_birth(self, value):
        # Если юзер регистрировался до введения этого поля и теперь
        # заполняет его в профиле — тоже нужно подтвердить 18+.
        return _validate_legal_age(value, 'Сервис доступен только пользователям 18 лет и старше.')


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Текущий пароль указан неверно.')
        return value

    def validate_new_password(self, value):
        user = self.context['request'].user
        try:
            django_validate_password(value, user=user)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value

    def validate(self, attrs):
        if attrs.get('old_password') == attrs.get('new_password'):
            raise serializers.ValidationError({'new_password': 'Новый пароль должен отличаться от текущего.'})
        return attrs

    def save(self, **kwargs):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save(update_fields=['password'])
        return user