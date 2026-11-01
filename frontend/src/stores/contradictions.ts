import { defineStore } from 'pinia'
import { ref } from 'vue'
import { contradictionsApi } from '@/api/contradictions'
import { useAuthStore } from '@/stores/auth'

// Счётчик новых активных противоречий в шапке («Противоречия 12»). Обновляется при переходах
// между страницами и после решения «Подтвердить / Отклонить».
export const useContradictionCount = defineStore('contradictionCount', () => {
  const count = ref<number | null>(null)

  async function refresh() {
    const auth = useAuthStore()
    if (auth.isGuest || !auth.can('contradictions.view')) {
      count.value = null
      return
    }
    try {
      count.value = (await contradictionsApi.count()).new
    } catch {
      count.value = null // счётчик — подсказка, ошибку не показываем
    }
  }

  return { count, refresh }
})
