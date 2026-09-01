# BistHub Frontend

Premium responsive storefront for the existing Django REST Framework API.

## Run locally

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

Django should run on `http://127.0.0.1:8000` by default. Change `VITE_API_URL` when the API is hosted elsewhere.

## Included

- 18+ age gate
- Home / collection landing page
- Product catalogue with search, strength filters and ordering
- Product details, gallery, stock, health warning and reviews
- JWT login / registration / automatic access-token refresh
- Shopping cart with quantity controls
- Checkout and order creation
- Stripe payment-session handoff when the API returns a checkout URL
- Order history and order details
- User profile editing
- Responsive desktop/mobile design

## Backend routes used

- `/api/shop/tobacco/`
- `/api/reviews/`
- `/api/accounts/*`
- `/api/cart/` and `/api/cart-items/`
- `/api/orders/`
- `/api/payments/`

## Production notes

Allow the frontend origin in Django CORS settings and set `VITE_API_URL` to the public API URL before building.
