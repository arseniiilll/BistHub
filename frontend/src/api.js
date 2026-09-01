const API_URL = (import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000/api').replace(/\/$/, '')

const tokens = {
  get access() { return localStorage.getItem('bisthub_access') },
  get refresh() { return localStorage.getItem('bisthub_refresh') },
  set(data) { localStorage.setItem('bisthub_access', data.access); localStorage.setItem('bisthub_refresh', data.refresh) },
  clear() { localStorage.removeItem('bisthub_access'); localStorage.removeItem('bisthub_refresh') }
}

function errorMessage(data) {
  if (!data) return 'Something went wrong.'
  if (typeof data === 'string') return data
  if (data.detail) return data.detail
  const first = Object.entries(data)[0]
  if (!first) return 'Something went wrong.'
  const value = Array.isArray(first[1]) ? first[1][0] : first[1]
  return `${first[0]}: ${typeof value === 'string' ? value : JSON.stringify(value)}`
}

async function refreshAccess() {
  if (!tokens.refresh) return false
  const res = await fetch(`${API_URL}/accounts/token/refresh/`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({refresh: tokens.refresh}) })
  if (!res.ok) { tokens.clear(); return false }
  const data = await res.json(); localStorage.setItem('bisthub_access', data.access); if (data.refresh) localStorage.setItem('bisthub_refresh', data.refresh); return true
}

async function request(path, options = {}, retry = true) {
  const headers = {...(options.body instanceof FormData ? {} : {'Content-Type':'application/json'}), ...(options.headers || {})}
  if (tokens.access) headers.Authorization = `Bearer ${tokens.access}`
  const res = await fetch(`${API_URL}${path}`, {...options, headers})
  if (res.status === 401 && retry && tokens.refresh && await refreshAccess()) return request(path, options, false)
  const text = await res.text(); const data = text ? JSON.parse(text) : null
  if (!res.ok) throw new Error(errorMessage(data))
  return data
}

export const api = {
  tokens,
  products: (query='') => request(`/shop/tobacco/${query ? `?${query}` : ''}`),
  product: slug => request(`/shop/tobacco/${encodeURIComponent(slug)}/`),
  register: body => request('/accounts/register/', {method:'POST', body:JSON.stringify(body)}),
  login: body => request('/accounts/login/', {method:'POST', body:JSON.stringify(body)}),
  me: () => request('/accounts/me/'),
  updateMe: body => request('/accounts/me/', {method:'PATCH', body: body instanceof FormData ? body : JSON.stringify(body)}),
  changePassword: body => request('/accounts/change-password/', {method:'POST', body:JSON.stringify(body)}),
  logout: () => request('/accounts/logout/', {method:'POST', body:JSON.stringify({refresh:tokens.refresh})}).finally(()=>tokens.clear()),
  cart: () => request('/cart/'),
  addCart: (product, quantity=1) => request('/cart-items/', {method:'POST', body:JSON.stringify({product,quantity})}),
  updateCart: (id, quantity) => request(`/cart-items/${id}/`, {method:'PATCH', body:JSON.stringify({quantity})}),
  removeCart: id => request(`/cart-items/${id}/`, {method:'DELETE'}),
  orders: () => request('/orders/'),
  order: id => request(`/orders/${id}/`),
  createOrder: body => request('/orders/', {method:'POST', body:JSON.stringify(body)}),
  payments: () => request('/payments/'),
  createPayment: (order_id, payment_method='card') => request('/payments/', {method:'POST', body:JSON.stringify({order_id,payment_method})}),
  createReview: body => request('/reviews/', {method:'POST', body:JSON.stringify(body)}),
  deleteReview: id => request(`/reviews/${id}/`, {method:'DELETE'})
}

export { API_URL }