/**
 * 认证中间件 - 需要登录才能访问
 * 
 * @author hienao
 * @date 2026-01-31
 */

import { useAuthStore } from '~/core/stores/auth'
import { apiCall } from '~/core/api/client'

export default defineNuxtRouteMiddleware(async (_to) => {
  // 只在客户端执行认证检查
  if (!import.meta.client) {
    return
  }

  const authStore = useAuthStore()

  // 恢复认证状态
  authStore.restoreAuth()
  if (!authStore.isAuthenticated) {
    const token = localStorage.getItem('token')
    if (token) {
      try {
        const result = await apiCall<{code:number,data:{token:string,username:string}}>('/auth/openlist-session',{method:'POST',body:{token}})
        if (result.code===200 && result.data?.token) authStore.setAuth(result.data.token,{username:result.data.username},false)
      } catch { /* The regular login page remains available if OpenList has expired. */ }
    }
  }

  console.log('[Auth Middleware] 认证状态检查:')
  console.log('- isAuthenticated:', authStore.isAuthenticated)

  // 检查是否已登录
  if (!authStore.isAuthenticated) {
    console.log('[Auth Middleware] 未认证，跳转登录页')
    return navigateTo('/auth/login', { replace: true })
  }

  console.log('[Auth Middleware] 认证通过，允许访问')
})
