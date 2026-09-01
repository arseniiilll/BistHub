from rest_framework import status, generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

from .serializers import (
    UserRegistrationSerializer,
    UserLoginSerializer,
    UserSerializer,
    UserUpdateSerializer,
    ChangePasswordSerializer,
)


class RegisterView(generics.CreateAPIView):
    """POST /api/accounts/register/"""
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'register'

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        refresh = RefreshToken.for_user(user)
        return Response({
            # context нужен, чтобы avatar (ImageField) сериализовался в
            # абсолютный URL — без request DRF отдаёт относительный путь,
            # и ответ /register/ отличался бы форматом от /me/.
            'user': UserSerializer(user, context=self.get_serializer_context()).data,
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        }, status=status.HTTP_201_CREATED)


class LoginView(generics.GenericAPIView):
    """POST /api/accounts/login/ — {email, password} -> access/refresh токены."""
    serializer_class = UserLoginSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        refresh = RefreshToken.for_user(user)
        return Response({
            'user': UserSerializer(user, context=self.get_serializer_context()).data,
            'refresh': str(refresh),
            'access': str(refresh.access_token),
        }, status=status.HTTP_200_OK)


class MeView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/accounts/me/ — профиль текущего пользователя."""
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method in ('PUT', 'PATCH'):
            return UserUpdateSerializer
        return UserSerializer

    def update(self, request, *args, **kwargs):
        # RetrieveUpdateAPIView.update() по умолчанию отвечает данными того же
        # сериализатора, что валидировал вход — UserUpdateSerializer, в котором
        # нет id/email/username/full_name/created_at. Из-за этого GET и
        # PATCH возвращали профиль в разной форме. Валидируем и сохраняем как
        # раньше, но в ответе всегда отдаём полный UserSerializer.
        super().update(request, *args, **kwargs)
        instance = self.get_object()
        output_serializer = UserSerializer(instance, context=self.get_serializer_context())
        return Response(output_serializer.data)


class ChangePasswordView(generics.GenericAPIView):
    """POST /api/accounts/change-password/ — {old_password, new_password}."""
    serializer_class = ChangePasswordSerializer
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        # Смена пароля должна завершать все остальные сессии: отзываем
        # (blacklist) все ранее выданные refresh-токены этого пользователя,
        # чтобы токены, украденные/оставленные на других устройствах,
        # больше не работали. Требует 'rest_framework_simplejwt.token_blacklist'
        # в INSTALLED_APPS (уже нужен для LogoutView).
        # bulk_create(ignore_conflicts=True) вместо get_or_create() в цикле —
        # один INSERT-запрос вместо SELECT+INSERT на каждый токен пользователя.
        outstanding_tokens = OutstandingToken.objects.filter(user=request.user)
        BlacklistedToken.objects.bulk_create(
            [BlacklistedToken(token=token) for token in outstanding_tokens],
            ignore_conflicts=True,
        )

        # Формулировка сознательно не говорит "на других устройствах": блэклист
        # выше отзывает ВСЕ OutstandingToken пользователя, включая refresh
        # текущей сессии тоже — она не привилегированная. Access-токен текущей
        # сессии при этом продолжит работать до истечения своего собственного
        # срока жизни (blacklist в simplejwt действует на refresh, не на access).
        return Response(
            {'detail': 'Пароль успешно изменён. Для повторного входа на всех устройствах, включая текущее, '
                       'понадобится новый логин после истечения текущего access-токена.'},
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """POST /api/accounts/logout/ — {refresh} -> добавляет refresh-токен в blacklist.
    Требует 'rest_framework_simplejwt.token_blacklist' в INSTALLED_APPS."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response({'detail': 'Поле refresh обязательно.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            token = RefreshToken(refresh_token)
        except (TokenError, InvalidToken):
            return Response({'detail': 'Невалидный или уже отозванный токен.'}, status=status.HTTP_400_BAD_REQUEST)

        # Без этой проверки аутентифицированный пользователь мог бы
        # заблэклистить чужой (но ещё валидный) refresh-токен, если он
        # каким-то образом стал ему известен — например, разлогинить
        # другого пользователя со всех его устройств.
        if str(token.get('user_id')) != str(request.user.id):
            return Response({'detail': 'Невалидный или уже отозванный токен.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            token.blacklist()
        except (TokenError, InvalidToken):
            return Response({'detail': 'Невалидный или уже отозванный токен.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'detail': 'Вы вышли из системы.'}, status=status.HTTP_200_OK)