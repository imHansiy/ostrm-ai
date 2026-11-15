/** Keep application requests inside its configured mount, separate from OpenList. */
export function resolveApiPath(endpoint: string): string {
  if (/^https?:\/\//i.test(endpoint)) return endpoint
  const base = String(useRuntimeConfig().public.apiBase || '/api').replace(/\/+$/, '')
  if (endpoint === base || endpoint.startsWith(base + '/')) return endpoint
  const path = endpoint.replace(/^\/api(?=\/|$)/, '').replace(/^\/+/, '')
  return `${base}/${path}`
}

export function resolveAppPath(path: string): string {
  return String(useRuntimeConfig().app.baseURL || '/').replace(/\/+$/, '') + '/' + path.replace(/^\/+/, '')
}
