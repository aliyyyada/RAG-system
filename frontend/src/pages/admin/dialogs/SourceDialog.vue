<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import AppDialog from '@/components/ui/AppDialog.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import { sourcesApi } from '@/api/sources'
import type { MssqlConfig, Source, SourceAdapter } from '@/api/types'

// Подключение источника и его настройки: адрес, расписание, ночная сверка.
// Для MS SQL Server ещё вызов функции и режим прав. Логин и пароль SQL Server
// в интерфейсе не вводятся: они только в .env (SOURCE_MSSQL_USER / SOURCE_MSSQL_PASSWORD).
const open = defineModel<boolean>('open', { required: true })
const props = defineProps<{ source: Source | null }>()
const emit = defineEmits<{ saved: [source: Source] }>()

const form = reactive({
  code: '',
  name: '',
  adapter: 'mssql' as SourceAdapter,
  base_url: 'mssql://',
  enabled: true,
  sync_interval_minutes: 60,
  overlap_minutes: 60,
  reconcile_hour: 2,
})
// Остальные поля MssqlConfig (имена колонок и т. п.) сохраняются как были.
const sql = reactive({
  call_sql: 'EXEC dbo.GetDocuments',
  service_user: '',
  file_type_column: '',
  access_mode: 'all_authenticated' as MssqlConfig['access_mode'],
  access_sql: '',
  common_sql: '',
  encrypt: true,
})
let restConfig: Record<string, unknown> = {}
const error = ref<string | null>(null)
const busy = ref(false)
const isSql = computed(() => form.adapter === 'mssql')

watch(open, (v) => {
  if (!v) return
  error.value = null
  const s = props.source
  const adapter = s?.adapter ?? 'mssql'
  Object.assign(form, {
    code: s?.code ?? '',
    name: s?.name ?? '',
    adapter,
    base_url: s?.base_url ?? 'mssql://',
    enabled: s?.enabled ?? true,
    sync_interval_minutes: s?.sync_interval_minutes ?? 60,
    overlap_minutes: s?.overlap_minutes ?? 60,
    reconcile_hour: s?.reconcile_hour ?? 2,
  })
  const c = (adapter === 'mssql' ? s?.config ?? {} : {}) as Partial<MssqlConfig>
  const { call_sql, service_user, file_type_column, access_mode, access_sql, common_sql, encrypt, ...rest } = c
  restConfig = rest
  Object.assign(sql, {
    call_sql: call_sql ?? 'EXEC dbo.GetDocuments',
    service_user: service_user ?? '',
    file_type_column: file_type_column ?? '',
    access_mode: access_mode ?? 'all_authenticated',
    access_sql: access_sql ?? '',
    common_sql: common_sql ?? '',
    encrypt: encrypt ?? true,
  })
})

watch(
  () => form.adapter,
  (a) => {
    if (props.source) return
    if (a === 'mssql' && !form.base_url.startsWith('mssql://')) form.base_url = 'mssql://'
    if (a === 'http_json' && !form.base_url.startsWith('https://')) form.base_url = 'https://'
  },
)

function config(): Record<string, unknown> | undefined {
  if (!isSql.value) return undefined
  const orNull = (v: string) => v.trim() || null
  return {
    ...restConfig,
    call_sql: sql.call_sql.trim(),
    service_user: orNull(sql.service_user),
    file_type_column: orNull(sql.file_type_column),
    access_mode: sql.access_mode,
    access_sql: sql.access_mode === 'sql' ? orNull(sql.access_sql) : null,
    common_sql: orNull(sql.common_sql),
    encrypt: sql.encrypt,
  }
}

async function save() {
  busy.value = true
  error.value = null
  try {
    const { code, adapter, ...settings } = form
    const body = { ...settings, config: config() }
    const saved = props.source
      ? await sourcesApi.update(props.source.id, body)
      : await sourcesApi.create({ code: code.trim(), adapter, ...body })
    open.value = false
    emit('saved', saved)
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось сохранить'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AppDialog v-model:open="open" :title="source ? 'Настройки источника' : 'Подключить источник'">
    <form id="source-form" class="stack" @submit.prevent="save">
      <div v-if="!source" class="field">
        <label class="label" for="src-code">Код</label>
        <input id="src-code" v-model="form.code" class="input mono" required pattern="[a-z][a-z0-9_\-]{1,31}" placeholder="docflow" />
        <span class="hint">Латиница, без пробелов. Используется в API для самого источника.</span>
      </div>
      <div class="field">
        <label class="label" for="src-name">Название</label>
        <input id="src-name" v-model="form.name" class="input" required maxlength="200" placeholder="Документооборот" />
      </div>
      <div class="field">
        <label class="label" for="src-adapter">Тип источника</label>
        <select id="src-adapter" v-model="form.adapter" class="select" :disabled="!!source">
          <option value="mssql">MS SQL Server (функция)</option>
          <option value="http_json">HTTPS API (JSON)</option>
        </select>
      </div>

      <template v-if="isSql">
        <div class="field">
          <label class="label" for="src-url">Сервер и база</label>
          <input id="src-url" v-model="form.base_url" class="input mono" required pattern="mssql://.+/.+" placeholder="mssql://sql.zavod.local:1433/Docflow" />
          <span class="hint">Логин и пароль SQL Server задаются только в .env: SOURCE_MSSQL_USER и SOURCE_MSSQL_PASSWORD.</span>
        </div>
        <div class="field">
          <label class="label" for="src-call">Вызов функции</label>
          <textarea id="src-call" v-model="sql.call_sql" class="input mono" rows="2" required />
          <span class="hint">Функция возвращает две таблицы: документы (ID_GUID, Номер, Дата, Тип, Тема…) и файлы (ID_GUID, BinaryFile, тип файла). Можно подставить :user и :since.</span>
        </div>
        <div class="row" style="gap: 16px; align-items: flex-start">
          <div class="field grow">
            <label class="label" for="src-user">Значение :user при синхронизации</label>
            <input id="src-user" v-model="sql.service_user" class="input mono" placeholder="не нужно" />
          </div>
          <div class="field grow">
            <label class="label" for="src-ftype">Колонка типа файла</label>
            <input id="src-ftype" v-model="sql.file_type_column" class="input mono" placeholder="определить автоматически" />
          </div>
        </div>
        <div class="field">
          <label class="label" for="src-access">Права на документы</label>
          <select id="src-access" v-model="sql.access_mode" class="select">
            <option value="all_authenticated">Любой вошедший видит все документы источника</option>
            <option value="sql">Спрашивать источник по табельному номеру</option>
          </select>
          <span class="hint">Без входа документы не видит никто.</span>
        </div>
        <div v-if="sql.access_mode === 'sql'" class="field">
          <label class="label" for="src-access-sql">Запрос прав</label>
          <textarea id="src-access-sql" v-model="sql.access_sql" class="input mono" rows="2" required placeholder="EXEC dbo.GetUserDocuments @Login = :user" />
          <span class="hint">:user — табельный номер. Ответ — список ID_GUID доступных документов.</span>
        </div>
        <div class="field">
          <label class="label" for="src-common-sql">Запрос общих документов</label>
          <textarea id="src-common-sql" v-model="sql.common_sql" class="input mono" rows="2" placeholder="необязательно" />
        </div>
        <label class="check"><input v-model="sql.encrypt" type="checkbox" /> Шифровать соединение (TLS, сертификат из SOURCE_CA_FILE)</label>
      </template>
      <div v-else class="field">
        <label class="label" for="src-url">Адрес API источника</label>
        <input id="src-url" v-model="form.base_url" class="input mono" required pattern="https://.+" />
        <span class="hint">Только https://. Сертификат источника проверяется по CA из папки certs/.</span>
      </div>

      <div class="row" style="gap: 16px; align-items: flex-start">
        <div class="field grow">
          <label class="label" for="src-interval">Синхронизация, каждые N минут</label>
          <input id="src-interval" v-model.number="form.sync_interval_minutes" class="input num" type="number" min="1" max="1440" required />
          <span v-if="isSql" class="hint">Функция отдаёт все файлы сразу, полный снимок берётся не чаще раза в 6 часов.</span>
        </div>
        <div class="field grow">
          <label class="label" for="src-hour">Ночная сверка, час</label>
          <input id="src-hour" v-model.number="form.reconcile_hour" class="input num" type="number" min="0" max="23" required />
        </div>
      </div>
      <div v-if="!isSql" class="field">
        <label class="label" for="src-overlap">Перекрытие окна, минут</label>
        <input id="src-overlap" v-model.number="form.overlap_minutes" class="input num" type="number" min="0" required />
        <span class="hint">Изменения берём с запасом назад, чтобы не потерять документ, записанный источником «задним числом».</span>
      </div>
      <label class="check"><input v-model="form.enabled" type="checkbox" /> Синхронизировать по расписанию</label>
      <FormError :message="error" />
    </form>
    <template #footer>
      <button class="btn btn-secondary" type="button" @click="open = false">Отмена</button>
      <button class="btn btn-primary" type="submit" form="source-form" :disabled="busy">
        {{ source ? 'Сохранить' : 'Подключить' }}
      </button>
    </template>
  </AppDialog>
</template>
