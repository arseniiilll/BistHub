from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.accounts.models import User
from apps.orders.models import Order

from .models import Payment
from .services import to_minor_units


class PaymentModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='buyer@example.com',
            username='buyer',
            password='StrongPassword123!',
            date_of_birth=date(1990, 1, 1),
        )
        self.order = Order.objects.create(
            user=self.user,
            first_name='Test',
            last_name='Buyer',
            email=self.user.email,
            total_price=Decimal('15.00'),
            status=Order.StatusChoices.PENDING,
        )

    def test_successful_payment_moves_pending_order_to_processing(self):
        payment = Payment.objects.create(
            user=self.user,
            order=self.order,
            amount=Decimal('15.00'),
            currency='RON',
            status=Payment.Status.PROCESSING,
        )

        payment.mark_as_succeeded()

        payment.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertIsNotNone(payment.processed_at)
        self.assertEqual(self.order.status, Order.StatusChoices.PROCESSING)

    def test_successful_payment_does_not_move_an_already_advanced_order_backwards(self):
        self.order.status = Order.StatusChoices.SHIPPED
        self.order.save(update_fields=['status'])
        payment = Payment.objects.create(
            user=self.user,
            order=self.order,
            amount=Decimal('15.00'),
            currency='RON',
            status=Payment.Status.PROCESSING,
        )

        payment.mark_as_succeeded()

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.StatusChoices.SHIPPED)

    def test_failed_payment_stores_failure_reason(self):
        payment = Payment.objects.create(
            user=self.user,
            order=self.order,
            amount=Decimal('15.00'),
            currency='RON',
            status=Payment.Status.PROCESSING,
        )

        payment.mark_as_failed(reason='Card declined')

        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.FAILED)
        self.assertEqual(payment.metadata['failure_reason'], 'Card declined')
        self.assertIsNotNone(payment.processed_at)


class MoneyConversionTests(TestCase):
    def test_ron_amount_is_converted_to_minor_units(self):
        self.assertEqual(to_minor_units(Decimal('15.00')), 1500)
        self.assertEqual(to_minor_units(Decimal('15.99')), 1599)

    def test_rounding_is_half_up(self):
        self.assertEqual(to_minor_units(Decimal('1.005')), 101)

    def test_zero_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            to_minor_units(Decimal('0.00'))
