# -*- coding: utf-8 -*-
import logging
from decimal import Decimal, ROUND_HALF_UP

import stripe
from django.conf import settings
from django.db import transaction, IntegrityError
from django.db.models import Sum
from django.urls import reverse

from .models import Payment, PaymentAttempt, Refund, WebhookEvent

stripe.api_key = settings.STRIPE_SECRET_KEY

logger = logging.getLogger(__name__)

# Сопоставление статусов Refund в Stripe (en-US написание "canceled")
# с нашими choices в модели Refund (STATUS_CHOICES использует "cancelled").
STRIPE_TO_LOCAL_REFUND_STATUS = {
    'succeeded': 'succeeded',
    'pending': 'pending',
    'failed': 'failed',
    'canceled': 'cancelled',
}

"""
Патч для apps/payment/services.py

1. Добавь этот helper где-нибудь в начале файла (после импортов, до класса
   WebhookService), например сразу после `import stripe`:
"""

import json


def _stripe_obj_to_dict(obj):
    """
    Универсально конвертирует stripe.StripeObject (и вложенные в нём
    объекты) в обычный Python dict, пригодный для сохранения в JSONField.

    НЕ полагается на конкретное имя метода (to_dict_recursive /
    _to_dict_recursive), которое отличается между версиями SDK stripe —
    вместо этого использует встроенную JSON-сериализацию самого объекта
    (str(stripe_object) у stripe.StripeObject всегда возвращает валидный
    JSON, включая вложенные объекты) и затем json.loads() обратно в dict.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return json.loads(str(obj))


"""
2. В process_event(), замени строку:

    'data': event['data'].to_dict_recursive() if 'data' in event else {},

   на:

    'data': _stripe_obj_to_dict(event['data']) if 'data' in event else {},
"""


def _get(obj, key, default=None):
    """
    Безопасный доступ к полю и у обычного dict, и у stripe.StripeObject.

    stripe.StripeObject НЕ поддерживает .get() (кидает AttributeError,
    т.к. __getattr__ пытается искать атрибут/ключ с именем 'get') — только
    доступ через 'in' + '[...]'. dict же поддерживает оба варианта. Этот
    helper работает одинаково для обоих типов, поэтому обработчики вебхуков
    можно не переписывать под конкретный тип объекта.
    """
    if obj is None:
        return default
    return obj[key] if key in obj else default


def to_minor_units(amount: Decimal) -> int:
    """
    Decimal-сумма в валюте -> целое число минорных единиц (центы/баны)
    с математическим округлением до ближайшего целого, половина — вверх
    (ROUND_HALF_UP), а не усечением (int(x*100) просто обрезает дробную
    часть, что для денег недопустимо: 10.005 -> 1000 вместо верных 1001).
    Это НЕ банковское округление (ROUND_HALF_EVEN) — если для расчётов
    в проекте по какой-то причине требуется именно оно, здесь это нужно
    будет явно поменять.

    Проверяет, что сумма не меньше 1 цента после округления.

    Аргументы:
        amount: Decimal сумма в основных единицах (доллары, евро и т.п.)

    Возвращает:
        int: сумма в минорных единицах (центы, баны и т.п.)

    Исключения:
        ValueError: если сумма меньше или равна нулю после округления
    """
    minor_units = int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if minor_units <= 0:
        raise ValueError(f"Сумма должна быть не менее $0.01, получено ${amount}")
    return minor_units


class PaymentService:
    """Создание Stripe Checkout Session для оплаты заказа."""

    @staticmethod
    def create_checkout_session(order, request):
        """
        Создание Stripe Checkout Session для оплаты заказа.

        Поток:
        1. Блокируем заказ (select_for_update)
        2. Проверяем права и существующие платежи
        3. Вычисляем сумму по позициям
        4. Создаём Payment в БД
        5. Отпускаем блокировку (конец транзакции)
        6. Создаём Stripe сессию (вне БД-блокировки)
        7. Обновляем Payment с Stripe ID

        Аргументы:
            order: Order объект
            request: HTTP request для построения callback URLs

        Возвращает:
            dict: {'checkout_url': str, 'session_id': str, 'payment_id': int}

        Исключения:
            ValueError: если заказ невалиден или ошибка при создании сессии
        """
        with transaction.atomic():
            order_locked = order.__class__.objects.select_for_update().get(pk=order.pk)

            # Проверяем права на заказ
            if order_locked.user_id != request.user.id:
                raise ValueError("Это не ваш заказ.")

            # Проверяем, нет ли уже активного или успешного платежа.
            # ВАЖНО: 'succeeded' здесь обязателен, а не только 'pending'/
            # 'processing'. PaymentCreateSerializer.validate() тоже проверяет
            # "заказ уже оплачен", но делает это ДО входа в эту залоченную
            # транзакцию — то есть без select_for_update(). Между проверкой
            # в сериализаторе и стартом этой транзакции вебхук может успеть
            # перевести другой платёж по этому же заказу в 'succeeded'.
            # Без повторной проверки статуса 'succeeded' именно здесь, под
            # локом, это TOCTOU-окно позволило бы создать второй платёж на
            # уже оплаченный заказ.
            existing = Payment.objects.filter(
                order=order_locked,
                status__in=[
                    'pending', 'processing',
                    'succeeded', 'partially_refunded', 'refunded',
                ],
            ).first()
            if existing:
                if existing.status in ('succeeded', 'partially_refunded', 'refunded'):
                    raise ValueError("Заказ уже оплачен.")
                raise ValueError(
                    "У этого заказа уже есть незавершённый платёж. "
                    "Пожалуйста, дождитесь его завершения или отмены."
                )

            items = list(order_locked.items.select_related('tobacco').all())
            if not items:
                raise ValueError("Нельзя оплатить пустой заказ.")

            # Построчная сумма
            items_total = sum((item.price * item.quantity for item in items), Decimal('0'))

            if items_total != order_locked.total_price:
                raise ValueError(
                    "Сумма заказа не совпадает с суммой позиций "
                    f"({order_locked.total_price} != {items_total}). "
                    "Оплата остановлена, требуется проверка заказа."
                )

            # Валидируем, что сумма не меньше 1 цента
            try:
                to_minor_units(items_total)
            except ValueError as e:
                raise ValueError(f"Некорректная сумма заказа: {e}")

            # Создаём Payment как pending
            payment = Payment.objects.create(
                user=request.user,
                order=order_locked,
                amount=items_total,
                currency=Payment.DEFAULT_CURRENCY,
                status='pending',
                payment_method=Payment.PaymentProvider.STRIPE,
                description=f"Payment for Order #{order_locked.id}",
            )

        # Вне транзакции создаём Stripe сессию
        try:
            # Используем reverse() вместо хардкода "/api/payments/.../success/":
            # путь однозначно берётся из urls.py (router basename='payment' +
            # detail-action 'success'/'cancel' в PaymentViewSet), поэтому при
            # изменении префикса подключения приложения в корневом urls.py
            # (например, /api/ -> /api/v1/) эти ссылки не разойдутся молча
            # с реальными эндпоинтами.
            success_url = request.build_absolute_uri(
                reverse('payment:payment-success', args=[payment.id])
            )
            cancel_url = request.build_absolute_uri(
                reverse('payment:payment-cancel', args=[payment.id])
            )

            line_items = [
                {
                    'price_data': {
                        'currency': payment.currency.lower(),
                        'unit_amount': to_minor_units(item.price),
                        'product_data': {'name': item.tobacco.name},
                    },
                    'quantity': item.quantity,
                }
                for item in items
            ]

            checkout_metadata = {
                'payment_id': str(payment.id),
                'order_id': str(order.id),
                'user_id': str(request.user.id),
            }

            session_kwargs = dict(
                payment_method_types=['card'],
                line_items=line_items,
                mode='payment',
                success_url=success_url,
                cancel_url=cancel_url,
                metadata=checkout_metadata,
                payment_intent_data={'metadata': checkout_metadata},
                idempotency_key=f"checkout-session-{payment.id}",
            )

            # Не передаём customer_email вовсе, если email недоступен —
            # явная передача customer_email=None в Stripe API менее надёжна,
            # чем просто отсутствие параметра.
            customer_email = getattr(order_locked, 'email', None)
            if customer_email:
                session_kwargs['customer_email'] = customer_email

            session = stripe.checkout.Session.create(**session_kwargs)

            # Успешно создали сессию, обновляем Payment
            payment.stripe_session_id = session.id
            payment.status = 'processing'
            payment.save(update_fields=['stripe_session_id', 'status', 'updated_at'])

            return {
                'checkout_url': session.url,
                'session_id': session.id,
                'payment_id': payment.id,
            }

        except stripe.error.StripeError as e:
            payment.mark_as_failed(reason=str(e))
            PaymentAttempt.objects.create(
                payment=payment,
                status=PaymentAttempt.Status.FAILED,
                error_message=str(e),
            )
            raise ValueError(f"Stripe error: {str(e)}")
        except Exception as e:
            payment.mark_as_failed(reason=f"Internal error: {e}")
            PaymentAttempt.objects.create(
                payment=payment,
                status=PaymentAttempt.Status.FAILED,
                error_message=str(e),
            )
            raise

class WebhookService:
    """Верификация подписи и обработка вебхуков Stripe."""

    @staticmethod
    def verify_and_parse(payload, sig_header):
        """
        Верифицировать подпись Stripe и распарсить payload.

        Аргументы:
            payload: bytes сырой payload из request.body
            sig_header: str заголовок Stripe-Signature

        Возвращает:
            dict: распарсенное событие Stripe

        Исключения:
            ValueError: если подпись или payload невалидны
        """
        try:
            return stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            raise ValueError(f"Invalid payload: {str(e)}")
        except stripe.error.SignatureVerificationError as e:
            raise ValueError(f"Invalid signature: {str(e)}")

    @staticmethod
    def process_event(event):
        """
        Обработать событие Stripe идемпотентно.

        КРИТИЧНО: использует select_for_update() для предотвращения
        двойной обработки конкурентных вебхуков.

        Аргументы:
            event: dict событие от Stripe

        Возвращает:
            WebhookEvent объект

        Исключения:
            Exception: если обработчик выбросил исключение
        """
        with transaction.atomic():
            try:
                # КРИТИЧНО (Postgres-специфично): get_or_create() обязательно
                # оборачивается в СВОЙ ВЛОЖЕННЫЙ transaction.atomic() (это
                # создаёт SAVEPOINT). Без этого при конкурентной вставке и
                # срабатывании unique-constraint на event_id весь ВНЕШНИЙ
                # transaction.atomic() блок помечается Postgres как aborted —
                # и следующий же запрос в except-ветке (WebhookEvent.objects
                # .get(...)) упал бы с "current transaction is aborted,
                # commands ignored until end of transaction block", вместо
                # того чтобы подхватить уже созданную вебхук-запись. Вложенный
                # atomic() ловит IntegrityError через ROLLBACK TO SAVEPOINT,
                # оставляя внешнюю транзакцию рабочей.
                with transaction.atomic():
                    webhook_event, created = WebhookEvent.objects.select_for_update().get_or_create(
                        event_id=event['id'],
                        defaults={
                            'provider': 'stripe',
                            'event_type': event['type'],
                            # .to_dict_recursive() обязателен: event['data'] — это
                            # stripe.StripeObject (Data), а не обычный dict.
                            # psycopg2/json.dumps не умеет его сериализовать в
                            # JSONField напрямую (TypeError: Object of type Data
                            # is not JSON serializable). to_dict_recursive()
                            # рекурсивно превращает всё дерево в примитивы
                            # (dict/list/str/int/...).
                            'data': _stripe_obj_to_dict(event['data']) if 'data' in event else {},
                            'status': 'pending',
                        }
                    )
            except IntegrityError:
                # Параллельная доставка — подхватываем уже созданную запись
                webhook_event = WebhookEvent.objects.select_for_update().get(event_id=event['id'])
                created = False

            # Если уже успешно обработано — идемпотентность
            if not created and webhook_event.status == 'processed':
                return webhook_event

            # Проверяем лимит попыток обработки
            if webhook_event.status == 'failed' and not webhook_event.should_retry():
                # Событие "застряло" в dead-letter — дальше только ручной
                # retry из админки. Логируем на уровне ERROR, чтобы это было
                # видно системе мониторинга/алертинга (иначе о зависшем
                # событии узнают только при ручном просмотре админки).
                logger.error(
                    "Webhook event %s (%s) исчерпал лимит попыток (%s) и "
                    "не будет обработан автоматически — требуется ручной "
                    "retry из админки",
                    webhook_event.event_id, webhook_event.event_type,
                    WebhookEvent.MAX_ATTEMPTS,
                )
                return webhook_event

            handler = WEBHOOK_HANDLERS.get(event['type'])
            if not handler:
                # Для событий без обработчика счётчик попыток не тратим —
                # обработка для них в принципе не предпринимается, инкремент
                # attempts здесь только искажал бы его смысл при повторных
                # доставках одного и того же неподдерживаемого типа события.
                webhook_event.status = 'ignored'
                webhook_event.save(update_fields=['status'])
                return webhook_event

            # Увеличиваем счётчик попыток (атомарно, через модельный метод) —
            # только когда реально собираемся вызвать обработчик.
            webhook_event.increment_attempts()

        # ВАЖНО: вызов обработчика — НАМЕРЕННО вне transaction.atomic() выше.
        # get_or_create() и increment_attempts() к этому моменту уже
        # закоммичены. Если бы handler() вызывался внутри того же atomic()
        # и выбросил исключение, то re-raise ниже пробросился бы за границу
        # этого блока — и Django откатил бы ВСЮ транзакцию целиком, включая
        # только что записанные webhook_event/attempts и даже сам
        # webhook_event.mark_as_failed() (он писался бы в той же
        # транзакции). В результате запись о неудачном событии вообще не
        # попадала бы в БД, а MAX_ATTEMPTS и ручной retry из админки
        # переставали бы работать. Вызывая handler() уже после выхода из
        # atomic(), гарантируем, что mark_as_failed() ниже фиксируется
        # независимо от того, что сделал (и откатил в своей собственной
        # транзакции) сам обработчик.
        try:
            event_data = event['data'] if 'data' in event else {}
            handler(event_data['object'] if 'object' in event_data else event)
        except Exception as e:
            webhook_event.mark_as_failed(str(e))
            raise
        else:
            webhook_event.mark_as_processed()

        return webhook_event

    @staticmethod
    def reprocess_failed(event_id):
        """
        Ручная переобработка конкретного failed-события.

        Аргументы:
            event_id: str уникальный ID события от Stripe

        Возвращает:
            WebhookEvent объект

        Исключения:
            ValueError: если превышен лимит попыток
            Exception: если обработчик выбросил исключение
        """
        with transaction.atomic():
            webhook_event = WebhookEvent.objects.select_for_update().get(event_id=event_id)

            # Если уже обработано — ничего не делаем
            if webhook_event.status == 'processed':
                return webhook_event

            # Если превышен лимит попыток
            if not webhook_event.should_retry():
                if webhook_event.status != 'failed':
                    raise ValueError(
                        f"Событие {event_id} имеет статус '{webhook_event.status}', "
                        "а не 'failed' — переобработка предназначена только для "
                        "неудачных событий."
                    )
                raise ValueError(
                    f"Событие {event_id} превысило максимум попыток ({WebhookEvent.MAX_ATTEMPTS}). "
                    "Требуется ручное вмешательство."
                )

            handler = WEBHOOK_HANDLERS.get(webhook_event.event_type)
            if not handler:
                # См. аналогичное замечание в process_event(): для событий
                # без обработчика попытку не засчитываем.
                webhook_event.status = 'ignored'
                webhook_event.save(update_fields=['status'])
                return webhook_event

            webhook_event.increment_attempts()
            event_data = webhook_event.data

        # См. подробный комментарий в WebhookService.process_event(): вызов
        # обработчика намеренно вынесен ЗА пределы transaction.atomic() выше,
        # чтобы исключение из handler() не откатывало заодно уже
        # закоммиченный increment_attempts() и последующий mark_as_failed().
        try:
            # webhook_event.data здесь — уже обычный dict (JSONField),
            # так как при сохранении выше он прошёл через to_dict_recursive(),
            # поэтому .get() тут работает нормально.
            handler(event_data.get('object', event_data))
        except Exception as e:
            webhook_event.mark_as_failed(str(e))
            raise
        else:
            webhook_event.mark_as_processed()

        return webhook_event

    @staticmethod
    def reprocess_failed(event_id):
        """
        Ручная переобработка конкретного failed-события.

        Аргументы:
            event_id: str уникальный ID события от Stripe

        Возвращает:
            WebhookEvent объект

        Исключения:
            ValueError: если превышен лимит попыток
            Exception: если обработчик выбросил исключение
        """
        with transaction.atomic():
            webhook_event = WebhookEvent.objects.select_for_update().get(event_id=event_id)

            # Если уже обработано — ничего не делаем
            if webhook_event.status == 'processed':
                return webhook_event

            # Если превышен лимит попыток
            if not webhook_event.should_retry():
                if webhook_event.status != 'failed':
                    raise ValueError(
                        f"Событие {event_id} имеет статус '{webhook_event.status}', "
                        "а не 'failed' — переобработка предназначена только для "
                        "неудачных событий."
                    )
                raise ValueError(
                    f"Событие {event_id} превысило максимум попыток ({WebhookEvent.MAX_ATTEMPTS}). "
                    "Требуется ручное вмешательство."
                )

            handler = WEBHOOK_HANDLERS.get(webhook_event.event_type)
            if not handler:
                # См. аналогичное замечание в process_event(): для событий
                # без обработчика попытку не засчитываем.
                webhook_event.status = 'ignored'
                webhook_event.save(update_fields=['status'])
                return webhook_event

            webhook_event.increment_attempts()
            event_data = webhook_event.data

        # См. подробный комментарий в WebhookService.process_event(): вызов
        # обработчика намеренно вынесен ЗА пределы transaction.atomic() выше,
        # чтобы исключение из handler() не откатывало заодно уже
        # закоммиченный increment_attempts() и последующий mark_as_failed().
        try:
            handler(event_data.get('object', event_data))
        except Exception as e:
            webhook_event.mark_as_failed(str(e))
            raise
        else:
            webhook_event.mark_as_processed()

        return webhook_event


def _handle_checkout_session_completed(session):
    """Обработка checkout.session.completed — платёж прошёл успешно."""
    metadata = _get(session, 'metadata') or {}
    payment_id = _get(metadata, 'payment_id')
    if not payment_id:
        return

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(id=payment_id)
        except Payment.DoesNotExist:
            return

        # Защита от повторной доставки события
        if payment.status == 'succeeded':
            return

        payment.stripe_payment_intent_id = _get(session, 'payment_intent')
        payment.mark_as_succeeded()

        # ВАЖНО: get_or_create() смотрит только на lookup-поля (payment,
        # stripe_payment_intent_id) — если запись уже существует (например,
        # раньше по этому же payment_intent пришёл payment_intent.payment_failed
        # для первой неудачной попытки оплаты, а клиент затем успешно
        # расплатился другой картой в той же Checkout Session), defaults
        # ИГНОРИРУЮТСЯ и вернётся старая запись со status='failed'. Явно
        # обновляем её до 'succeeded', иначе итоговая успешная попытка
        # молча осталась бы помечена как неудачная.
        attempt, created = PaymentAttempt.objects.get_or_create(
            payment=payment,
            stripe_payment_intent_id=_get(session, 'payment_intent'),
            defaults={
                'status': PaymentAttempt.Status.SUCCEEDED,
            }
        )
        if not created and attempt.status != PaymentAttempt.Status.SUCCEEDED:
            attempt.status = PaymentAttempt.Status.SUCCEEDED
            attempt.save(update_fields=['status'])


def _handle_checkout_session_expired(session):
    """Обработка checkout.session.expired — сессия истекла."""
    metadata = _get(session, 'metadata') or {}
    payment_id = _get(metadata, 'payment_id')
    if not payment_id:
        return

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(id=payment_id)
        except Payment.DoesNotExist:
            return

        # Если платёж ещё в процессе — отмечаем как отменённый
        if payment.status in ('pending', 'processing'):
            payment.mark_as_cancelled()


def _handle_payment_intent_payment_failed(payment_intent):
    """
    Обработка payment_intent.payment_failed — отказ ОДНОЙ ИЗ ПОПЫТОК оплаты.

    ВАЖНО: этот обработчик НЕ переводит Payment.status в 'failed'. Один
    Checkout Session/PaymentIntent может пережить несколько попыток оплаты
    (клиент ввёл невалидную карту, получил этот ивент, затем успешно
    расплатился другой картой в той же сессии — см. комментарий к
    PaymentAttempt в models.py про "несколько charges в рамках одного
    intent"). Если бы этот обработчик сразу финализировал Payment как
    'failed', то на время, пока сессия ещё жива и клиент может повторить
    оплату, заказ выглядел бы как проваленный — а
    PaymentCreateSerializer.validate() и
    PaymentService.create_checkout_session() (проверяющие статусы платежей
    заказа) разрешили бы создать ВТОРОЙ платёж/сессию на тот же заказ. Если
    после этого первая сессия всё же завершится успехом (checkout.session.
    completed не проверяет status на 'failed'), заказ окажется оплачен
    дважды.

    Финализация в 'failed'/'cancelled' происходит только там, где сессия
    действительно закрывается: checkout.session.expired,
    checkout.session.async_payment_failed (см. обработчики ниже), либо при
    ошибке создания самой Stripe-сессии в
    PaymentService.create_checkout_session().

    Ищет платёж сначала по payment_id в metadata, затем по stripe_payment_intent_id.
    """
    stripe_pi_id = _get(payment_intent, 'id')

    with transaction.atomic():
        payment = None
        metadata = _get(payment_intent, 'metadata') or {}
        payment_id = _get(metadata, 'payment_id')

        # Пытаемся найти по payment_id из metadata
        if payment_id:
            try:
                payment = Payment.objects.select_for_update().get(id=payment_id)
            except Payment.DoesNotExist:
                pass

        # Если не нашли — пытаемся по stripe_payment_intent_id
        if payment is None and stripe_pi_id:
            try:
                payment = Payment.objects.select_for_update().get(
                    stripe_payment_intent_id=stripe_pi_id
                )
            except Payment.DoesNotExist:
                pass

        if payment is None:
            return

        # Не трогаем платёж, если он уже в финальном состоянии (по другой причине)
        if payment.status in ('succeeded', 'failed', 'cancelled', 'refunded', 'partially_refunded'):
            return

        # Сохраняем stripe_payment_intent_id, если он ещё не был записан —
        # чтобы последующие вебхуки по этому intent (в т.ч. без payment_id
        # в metadata) находили платёж по stripe_payment_intent_id.
        if stripe_pi_id and not payment.stripe_payment_intent_id:
            payment.stripe_payment_intent_id = stripe_pi_id
            payment.save(update_fields=['stripe_payment_intent_id', 'updated_at'])

        error = _get(payment_intent, 'last_payment_error') or {}
        reason = _get(error, 'message', 'Unknown error')

        # См. аналогичное замечание в _handle_checkout_session_completed:
        # get_or_create() игнорирует defaults, если запись уже есть (повторный
        # payment_intent.payment_failed для той же попытки/intent). Обновляем
        # error_message свежим текстом при каждой неудачной попытке, но
        # никогда не откатываем attempt, если он почему-то уже помечен как
        # 'succeeded' (не должно происходить при штатном порядке событий, но
        # проверяем явно, а не полагаемся на порядок доставки вебхуков).
        attempt, created = PaymentAttempt.objects.get_or_create(
            payment=payment,
            stripe_payment_intent_id=stripe_pi_id,
            defaults={
                'status': PaymentAttempt.Status.FAILED,
                'error_message': reason,
            }
        )
        if not created and attempt.status != PaymentAttempt.Status.SUCCEEDED:
            attempt.status = PaymentAttempt.Status.FAILED
            attempt.error_message = reason
            attempt.save(update_fields=['status', 'error_message'])


def _handle_checkout_session_async_payment_failed(session):
    """
    Обработка checkout.session.async_payment_failed — асинхронный отказ
    (например, при 3D Secure).
    """
    metadata = _get(session, 'metadata') or {}
    payment_id = _get(metadata, 'payment_id')
    if not payment_id:
        return

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(id=payment_id)
        except Payment.DoesNotExist:
            return

        if payment.status in ('pending', 'processing'):
            error_msg = f"Асинхронный отказ платежа (статус: {_get(session, 'status', 'unknown')})"
            payment.mark_as_failed(reason=error_msg)


def _handle_charge_refunded(charge):
    """
    Обработка charge.refunded — по charge был оформлен возврат.

    Возврат мог быть инициирован не через наш RefundService (например,
    вручную из Stripe Dashboard) — без этого блока статус платежа менялся
    бы, а строки в Refund не появлялось, и история возвратов расходилась
    бы с реальностью.

    ВАЖНО: докачка страниц возвратов через stripe.Refund.list() — это
    сетевой вызов, поэтому он выполняется ДО открытия транзакции и
    получения select_for_update() на платёж. Раньше это делалось внутри
    залоченной транзакции, из-за чего строка платежа могла оставаться
    заблокированной на время сетевого round-trip к Stripe — нежелательно
    при всплеске вебхуков.
    """
    payment_intent_id = _get(charge, 'payment_intent')
    if not payment_intent_id:
        return

    # Собираем полный список возвратов ВНЕ блокировки БД.
    refunds_data = _get(charge, 'refunds') or {}
    refund_list = list(_get(refunds_data, 'data', []))

    if _get(refunds_data, 'has_more'):
        # Если возвратов больше — довыгружаем остальные
        try:
            refund_list = list(stripe.Refund.list(
                payment_intent=payment_intent_id,
                limit=100
            ).auto_paging_iter())
        except stripe.error.StripeError:
            # Если не можем довыгрузить — обрабатываем то, что уже есть
            pass

    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(
                stripe_payment_intent_id=payment_intent_id
            )
        except Payment.DoesNotExist:
            return

        for stripe_refund in refund_list:
            stripe_refund_id = _get(stripe_refund, 'id')
            if not stripe_refund_id:
                continue

            # КРИТИЧНО: у stripe_refund есть собственный статус (succeeded/
            # pending/failed/canceled) — его нужно смаппить через
            # STRIPE_TO_LOCAL_REFUND_STATUS, а НЕ считать любой пришедший
            # в этом списке возврат автоматически успешным. Иначе ещё не
            # проведённый (pending) или отклонённый (failed) в Stripe возврат
            # будет ошибочно записан как succeeded, что исказит total_refunded
            # и статус платежа ниже.
            local_status = STRIPE_TO_LOCAL_REFUND_STATUS.get(
                _get(stripe_refund, 'status'), 'pending'
            )

            refund_amount = Decimal(_get(stripe_refund, 'amount', 0)) / 100

            try:
                refund = Refund.objects.select_for_update().get(
                    stripe_refund_id=stripe_refund_id
                )
                created = False
            except Refund.DoesNotExist:
                # Возможен случай, когда RefundService.create_refund() успешно
                # вызвал Stripe, но упал (сеть/процесс) до того, как записал
                # stripe_refund_id и статус локально — в БД остаётся "осиротевший"
                # Refund со status='pending' и stripe_refund_id=None, который
                # навсегда резервирует сумму в refundable_amount и никогда не
                # синхронизируется. Пытаемся усыновить такую запись, а не
                # создавать рядом дубликат — иначе один и тот же реальный
                # возврат в Stripe задвоится в наших данных.
                orphan = None

                # Приоритетный, точный способ: RefundService.create_refund()
                # сам передаёт в Stripe metadata={'refund_id': <наш PK>, ...}
                # при создании возврата — Stripe возвращает эту metadata
                # обратно в объекте Refund. Если она есть, это надёжная прямая
                # ссылка на конкретную локальную запись, без риска перепутать
                # два разных pending-возврата одинаковой суммы по одному платежу.
                refund_metadata = _get(stripe_refund, 'metadata') or {}
                local_refund_id = _get(refund_metadata, 'refund_id')
                if local_refund_id:
                    try:
                        orphan = Refund.objects.select_for_update().filter(
                            pk=int(local_refund_id),
                            stripe_refund_id__isnull=True,
                        ).first()
                    except (TypeError, ValueError):
                        orphan = None

                if orphan is None:
                    # Fallback для возвратов, оформленных НЕ через RefundService
                    # (например, вручную из Stripe Dashboard — у них нет нашей
                    # metadata) или если по какой-то причине metadata не пришла.
                    # Здесь уже приходится сопоставлять по (платёж, сумма,
                    # ещё не привязан к Stripe) — менее точно, но лучше, чем
                    # создавать дубликат.
                    # status__in=('pending', 'failed'): в норме orphan-запись
                    # всегда 'pending' (см. RefundService.create_refund). Но
                    # для устойчивости к уже существующим в БД записям, у
                    # которых локальный сбой после успешного возврата в Stripe
                    # произошёл ДО того, как код выше стал сохранять
                    # stripe_refund_id сразу после ответа Stripe, допускаем и
                    # 'failed' — иначе такие записи никогда не усыновятся и
                    # будут задвоены.
                    orphan = Refund.objects.select_for_update().filter(
                        payment=payment,
                        stripe_refund_id__isnull=True,
                        status__in=('pending', 'failed'),
                        amount=refund_amount,
                    ).order_by('created_at').first()

                if orphan:
                    orphan.stripe_refund_id = stripe_refund_id
                    orphan.save(update_fields=['stripe_refund_id'])
                    refund = orphan
                    created = False
                else:
                    refund = Refund.objects.create(
                        payment=payment,
                        amount=refund_amount,
                        reason='Возврат оформлен вне системы (Stripe Dashboard / внешний источник).',
                        status=local_status,
                        stripe_refund_id=stripe_refund_id,
                    )
                    created = True

            if created:
                if local_status == 'succeeded':
                    refund.mark_as_succeeded()
                elif local_status == 'failed':
                    refund.mark_as_failed(
                        reason='Возврат отмечен как неуспешный по данным Stripe (charge.refunded).'
                    )
                # 'pending'/'cancelled' уже сохранены значением status= при
                # создании — ничего дополнительно делать не нужно.
            elif refund.status != local_status:
                # Запись уже существовала (например, раньше пришла как pending),
                # но статус в Stripe с тех пор изменился — синхронизируем,
                # иначе локальные данные разойдутся с реальностью навсегда.
                if local_status == 'succeeded':
                    refund.mark_as_succeeded()
                elif local_status == 'failed':
                    refund.mark_as_failed(
                        reason='Статус возврата обновлён Stripe (charge.refunded).'
                    )
                else:
                    refund.status = local_status
                    refund.save(update_fields=['status'])

        # Обновляем статус платежа если были возвраты.
        # ПРИМЕЧАНИЕ: намеренно не обрабатывается случай УМЕНЬШЕНИЯ
        # total_refunded (например, ранее засчитанный возврат "развернули"
        # на стороне Stripe) — статус платежа в таком редком сценарии не
        # откатывается автоматически назад и потребует ручной проверки.
        total_refunded = payment.refunds.filter(status='succeeded').aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0')

        if total_refunded >= payment.amount:
            payment.mark_as_fully_refunded()
        elif total_refunded > Decimal('0'):
            payment.mark_as_partially_refunded()


# Обработчики вебхуков
WEBHOOK_HANDLERS = {
    'checkout.session.completed': _handle_checkout_session_completed,
    'checkout.session.expired': _handle_checkout_session_expired,
    'checkout.session.async_payment_failed': _handle_checkout_session_async_payment_failed,
    'payment_intent.payment_failed': _handle_payment_intent_payment_failed,
    'charge.refunded': _handle_charge_refunded,
}


class RefundService:
    """Создание возврата средств через Stripe."""

    @staticmethod
    def create_refund(payment, amount, reason, created_by):
        """
        Создание возврата средств с полной защитой от race conditions.

        Поток:
        1. Блокируем платёж (select_for_update)
        2. Проверяем возможность возврата
        3. Проверяем остаток с учётом pending возвратов
        4. Создаём запись Refund со статусом pending
        5. Отпускаем блокировку
        6. Вызываем Stripe Refund.create (вне БД-блокировки)
        7. Обновляем Refund с Stripe ID и статусом succeeded
        8. Обновляем статус Payment (повторно блокируя платёж)

        Аргументы:
            payment: Payment объект
            amount: Decimal сумма возврата
            reason: str причина возврата
            created_by: User объект, кто создал возврат

        Возвращает:
            Refund объект

        Исключения:
            ValueError: если платёж не может быть возвращён или ошибка Stripe
        """
        with transaction.atomic():
            payment_locked = Payment.objects.select_for_update().get(pk=payment.pk)

            # Проверяем, может ли быть возвращён
            if not payment_locked.can_be_refunded:
                raise ValueError(
                    f"Этот платёж нельзя вернуть. Статус: {payment_locked.status}, "
                    f"метод: {payment_locked.payment_method}"
                )

            # КРИТИЧНО: учитываем и pending возвраты (они резервируют сумму)
            already_reserved = payment_locked.refunds.filter(
                status__in=['succeeded', 'pending']
            ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

            if already_reserved + amount > payment_locked.amount:
                raise ValueError(
                    f"Сумма возврата превышает доступный остаток платежа. "
                    f"Зарезервировано: ${already_reserved}, запрашиваемый возврат: ${amount}, "
                    f"всего по платежу: ${payment_locked.amount}"
                )

            # Валидируем сумму возврата
            try:
                to_minor_units(amount)
            except ValueError as e:
                raise ValueError(f"Некорректная сумма возврата: {e}")

            # Создаём Refund как pending
            refund_record = Refund.objects.create(
                payment=payment_locked,
                amount=amount,
                reason=reason,
                created_by=created_by,
                status='pending',
            )

        # Вне транзакции/блокировки вызываем Stripe.
        # ВАЖНО: этот try/except охватывает ТОЛЬКО сам вызов stripe.Refund.create().
        # Пока мы внутри него, возврат в Stripe ещё не создан (или создание
        # точно упало) — поэтому здесь безопасно помечать refund_record как
        # 'failed'. Всё, что происходит ПОСЛЕ успешного возврата (сохранение
        # stripe_refund_id, mark_as_succeeded(), обновление статуса Payment),
        # вынесено в отдельный блок ниже: сбой на этом этапе не должен
        # откатывать запись в 'failed', т.к. возврат уже реально существует
        # в Stripe.
        try:
            stripe_refund = stripe.Refund.create(
                payment_intent=payment_locked.stripe_payment_intent_id,
                amount=to_minor_units(amount),
                reason='requested_by_customer',
                metadata={
                    'refund_id': str(refund_record.id),
                    'payment_id': str(payment_locked.id),
                },
                idempotency_key=f"refund-{refund_record.id}",
            )
        except stripe.error.StripeError as e:
            refund_record.mark_as_failed(reason=str(e))
            raise ValueError(f"Stripe refund error: {str(e)}")
        except Exception as e:
            refund_record.mark_as_failed(reason=str(e))
            raise ValueError(f"Refund creation error: {str(e)}")

        # Возврат в Stripe успешно создан. Сохраняем stripe_refund_id ОТДЕЛЬНЫМ
        # немедленным save() — до любых дальнейших шагов. Это гарантирует, что
        # даже если следующий шаг (mark_as_succeeded/обновление статуса Payment)
        # упадёт, запись всё равно будет находима по stripe_refund_id: вебхук
        # charge.refunded ищет Refund именно по этому полю в первую очередь
        # (см. _handle_charge_refunded) и, найдя её, сам доведёт статус до
        # 'succeeded'. Если бы мы не сохранили stripe_refund_id заранее и упали
        # бы ниже, orphan-поиск в вебхуке (который ищет status='pending' И
        # stripe_refund_id IS NULL) либо не нашёл бы запись, либо — что хуже —
        # запись оказалась бы помечена 'failed', под этот фильтр тоже не
        # попадающая, и вебхук создал бы дублирующий Refund.
        try:
            refund_record.stripe_refund_id = stripe_refund.id
            refund_record.save(update_fields=['stripe_refund_id'])
        except Exception as e:
            # Возврат в Stripe уже реально существует (id=stripe_refund.id),
            # даже если именно эту запись не удалось сохранить локально —
            # поэтому НЕ мержим это с исключениями до вызова stripe.Refund.create()
            # выше (там refund_record ещё безопасно помечать как 'failed').
            # Здесь запись остаётся 'pending' без stripe_refund_id — вебхук
            # charge.refunded подхватит её через orphan-поиск по (payment,
            # сумма, ещё не привязан к Stripe) и сам доведёт статус до 'succeeded'.
            logger.exception(
                "Refund %s создан в Stripe (id=%s), но не удалось сохранить "
                "stripe_refund_id локально — требуется проверка",
                refund_record.id, stripe_refund.id,
            )
            raise ValueError(
                f"Возврат создан в Stripe (id={stripe_refund.id}), но при "
                f"сохранении локальной записи произошла ошибка: {e}. "
                "Средства были возвращены; статус синхронизируется вебхуком "
                "либо требует ручной проверки."
            )

        try:
            refund_record.mark_as_succeeded()

            # Обновляем статус платежа если это полный возврат.
            # ВАЖНО: здесь снова берём select_for_update() на платёж, а не
            # просто refresh_from_db() без лока — иначе при двух параллельно
            # финализируемых возвратах по одному платежу возможен lost update
            # статуса payment.status (оба процесса читают и пишут статус без
            # взаимного исключения).
            with transaction.atomic():
                payment_locked = Payment.objects.select_for_update().get(pk=payment_locked.pk)
                total_refunded = payment_locked.refunds.filter(
                    status='succeeded'
                ).aggregate(total=Sum('amount'))['total'] or Decimal('0')

                if total_refunded >= payment_locked.amount:
                    payment_locked.mark_as_fully_refunded()
                else:
                    payment_locked.mark_as_partially_refunded()

            return refund_record

        except Exception as e:
            # НЕ вызываем refund_record.mark_as_failed() здесь: возврат уже
            # реально проведён в Stripe (id=stripe_refund.id), помечать его
            # как 'failed' было бы неверно и ломало бы синхронизацию с
            # реальностью. Запись остаётся со status='pending' и уже
            # сохранённым stripe_refund_id — вебхук charge.refunded подхватит
            # её напрямую и сам выставит 'succeeded'. Пробрасываем ошибку
            # дальше, чтобы вызывающий код (например, admin action) увидел,
            # что требуется проверка локального состояния.
            logger.exception(
                "Refund %s создан в Stripe (id=%s), но локальное обновление "
                "статуса после этого не удалось — требуется проверка",
                refund_record.id, stripe_refund.id,
            )
            raise ValueError(
                f"Возврат создан в Stripe (id={stripe_refund.id}), но при "
                f"обновлении локального статуса произошла ошибка: {e}. "
                "Средства были возвращены; статус синхронизируется вебхуком "
                "либо требует ручной проверки."
            )