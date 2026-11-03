<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import FormError from '@/components/ui/FormError.vue'
import { ApiError } from '@/api/client'
import {
  epozApi,
  type Okpd2Edition,
  type Okpd2Lookup,
  type EpozClauseResult,
  type EpozPartInfo,
  type EpozReport,
  type EpozStatus,
  type EpozUploadResult,
} from '@/api/epoz'
import { formatDateTime } from '@/utils/format'

// ЕПоЗ (проверка закупочного пакета): комплект из Положения и 14 приложений.
// Новая редакция сначала загружается как ЧЕРНОВИК: администратор видит отчёт разбора
// (нумерация пунктов, найденные ссылки) и только потом делает её действующей.
// Действующие документы защищены: удалить, исключить из поиска или переразобрать их нельзя.
const st = ref<EpozStatus | null>(null)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const dragging = ref(false)
const results = ref<EpozUploadResult[]>([])
const force = ref(false)
const picked = ref<number[]>([])
const customerGroup = ref<'' | 'I' | 'II'>('')
const clausePart = ref('main')
const clauseLabel = ref('10.3')
const clauseDraft = ref(false)
const clauseResult = ref<EpozClauseResult | null>(null)
const okpd = ref<Okpd2Edition | null>(null)
const okpdQuery = ref('')
const okpdResult = ref<Okpd2Lookup | null>(null)

function apply(s: EpozStatus) {
  st.value = s
  picked.value = [...s.group_ids]
  customerGroup.value = s.customer_group ?? ''
}

async function load() {
  try {
    apply(await epozApi.status())
    error.value = null
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось загрузить состояние ЕПоЗ'
  }
}
async function loadOkpd() {
  try {
    okpd.value = (await epozApi.okpd2()).active
  } catch {
    /* карточка справочника необязательна для остальной страницы */
  }
}
onMounted(() => {
  void load()
  void loadOkpd()
})

async function act(fn: () => Promise<string | void>) {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const msg = await fn()
    if (msg) notice.value = msg
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : 'Не удалось выполнить действие'
  } finally {
    busy.value = false
  }
}

function upload(files: File[]) {
  const docx = files.filter((f) => f.name.toLowerCase().endsWith('.docx'))
  if (!docx.length) {
    error.value = 'Нужны файлы Word (.docx): Положение и приложения'
    return
  }
  void act(async () => {
    const form = new FormData()
    docx.forEach((f) => form.append('files', f))
    const r = await epozApi.upload(form)
    results.value = r.results
    apply(r)
    const ok = r.results.filter((x) => x.ok).length
    return `Загружено файлов: ${ok} из ${r.results.length}. Проверьте отчёт черновика ниже.`
  })
}
const onPick = (e: Event) => {
  const input = e.target as HTMLInputElement
  upload(Array.from(input.files ?? []))
  input.value = ''
}
const onDrop = (e: DragEvent) => {
  dragging.value = false
  upload(Array.from(e.dataTransfer?.files ?? []))
}

const activate = () =>
  act(async () => {
    apply(await epozApi.activate(force.value))
    force.value = false
    return 'Редакция действующая. Документы защищены от удаления, доступ открыт выбранной группе.'
  })
const cancel = () =>
  act(async () => {
    if (!window.confirm('Отменить черновик? Файлы, загруженные только в него, будут удалены.')) return
    await epozApi.cancelDraft()
    results.value = []
    await load()
    return 'Черновик отменён.'
  })
const saveGroups = () =>
  act(async () => {
    apply(await epozApi.setGroups(picked.value))
    return 'Доступ к ЕПоЗ обновлён для всего комплекта.'
  })
const saveCustomer = () =>
  act(async () => {
    apply(await epozApi.setSettings(customerGroup.value || null))
    return 'Группа заказчика сохранена.'
  })
const onOkpd = (e: Event) => {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (!f) return
  void act(async () => {
    const form = new FormData()
    form.append('file', f)
    const r = await epozApi.okpd2Import(form)
    await loadOkpd()
    const warn = r.warnings.length ? ' ' + r.warnings.join(' ') : ''
    return (r.status === 'unchanged' ? 'Эта редакция справочника уже загружена.' : `Справочник загружен: ${r.records} кодов.`) + warn
  })
}
const findOkpd = () =>
  act(async () => {
    const q = okpdQuery.value.trim()
    okpdResult.value = /^[\d.\s]+$/.test(q) ? await epozApi.okpd2Lookup({ code: q }) : await epozApi.okpd2Lookup({ q })
  })
const findClause = () =>
  act(async () => {
    clauseResult.value = await epozApi.clauses(clausePart.value, clauseLabel.value.trim(), clauseDraft.value)
  })

const BADGE = {
  ok: { cls: 'b-ok', text: 'Разобрано' },
  warning: { cls: 'b-warn', text: 'Проверьте' },
  error: { cls: 'b-danger', text: 'Ошибка' },
  none: { cls: 'b-neutral', text: 'Не загружено' },
} as const
const badge = (p: EpozPartInfo) => (p.present ? BADGE[p.status ?? 'ok'] : BADGE.none)
const partRows = (r: EpozReport) => Object.entries(r.parts)
const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`)
const partKeys = computed(() => (st.value ? Object.keys(st.value.part_titles) : []))
const toggleGroup = (id: number) => {
  picked.value = picked.value.includes(id) ? picked.value.filter((x) => x !== id) : [...picked.value, id]
}
</script>

<template>
  <div class="page">
    <div class="page-h">
      <div class="grow">
        <h1>ЕПоЗ</h1>
        <p>Единое положение о закупке и 14 приложений: по ним работают общий поиск и проверка закупочного пакета.</p>
      </div>
    </div>
    <FormError :message="error" style="margin-bottom: 12px" />
    <div v-if="notice" class="alert a-ok" style="margin-bottom: 12px" role="status"><AppIcon name="check" /><div>{{ notice }}</div></div>

    <template v-if="st">
      <div class="tiles" style="margin-bottom: 16px">
        <div class="tile t-ok">
          <div class="l">Действующая редакция</div>
          <div class="n">{{ st.active ? '№' + st.active.id : '—' }}</div>
          <div class="d">{{ st.active?.activated_at ? formatDateTime(st.active.activated_at) : 'не загружена' }}</div>
        </div>
        <div class="tile">
          <div class="l">Черновик</div>
          <div class="n">{{ st.draft ? '№' + st.draft.id : 'нет' }}</div>
          <div class="d">{{ st.draft ? (st.draft.report.can_activate ? 'готов к активации' : 'есть замечания') : 'загрузите файлы ниже' }}</div>
        </div>
        <div class="tile">
          <div class="l">Группы с доступом</div>
          <div class="n">{{ st.group_ids.length }}</div>
          <div class="d">на весь комплект</div>
        </div>
        <div class="tile">
          <div class="l">Группа заказчика</div>
          <div class="n">{{ st.customer_group ?? '—' }}</div>
          <div class="d">{{ st.customer_group ? 'настройка организации' : 'не задана' }}</div>
        </div>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h"><h3 class="grow">Загрузка новой редакции</h3><span class="small muted">черновик наследует части действующей редакции</span></div>
        <div class="card-b">
          <label
            class="dropzone"
            :class="{ on: dragging }"
            data-testid="epoz-drop"
            @dragover.prevent="dragging = true"
            @dragleave="dragging = false"
            @drop.prevent="onDrop"
          >
            <AppIcon name="down" />
            <div>
              <b>Перетащите файлы Word сюда</b> или выберите на компьютере.
              <div class="small muted">Положение и приложения 1–14 (.docx). Часть комплекта определяется по имени файла. «Перечень изменений» не загружается.</div>
            </div>
            <input type="file" multiple accept=".docx" style="display: none" data-testid="epoz-file" @change="onPick" />
          </label>
          <table v-if="results.length" class="tbl" style="margin-top: 12px">
            <thead><tr><th>Файл</th><th>Часть</th><th>Результат</th></tr></thead>
            <tbody>
              <tr v-for="r in results" :key="r.file">
                <td>{{ r.file }}</td>
                <td>{{ r.part ? st.part_titles[r.part] : '—' }}</td>
                <td>
                  <span class="badge" :class="r.ok ? 'b-ok' : 'b-danger'">{{ r.ok ? 'Загружено' : 'Не загружено' }}</span>
                  <span v-if="r.message" class="small muted"> {{ r.message }}</span>
                  <span v-for="m in r.messages ?? []" :key="m" class="small muted"> {{ m }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="st.draft" class="card" style="margin-bottom: 16px" data-testid="epoz-draft">
        <div class="card-h">
          <h3 class="grow">Отчёт черновика №{{ st.draft.id }}</h3>
          <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="cancel">Отменить черновик</button>
        </div>
        <div class="card-b">
          <div v-for="b in st.draft.report.blockers" :key="b" class="alert a-danger" style="margin-bottom: 8px"><AppIcon name="warn" /><div>{{ b }}</div></div>
          <div v-for="w in st.draft.report.warnings" :key="w" class="alert a-warn" style="margin-bottom: 8px"><AppIcon name="warn" /><div>{{ w }}</div></div>
          <div v-if="st.draft.report.can_activate" class="alert a-ok" style="margin-bottom: 8px"><AppIcon name="check" /><div>Нумерация проверена, пункты для критериев найдены. Черновик можно сделать действующим.</div></div>
          <table class="tbl">
            <thead>
              <tr><th>Часть комплекта</th><th>Файл</th><th class="r">Фрагментов</th><th class="r">Пунктов</th><th class="r">Ссылок найдено</th><th>Статус</th></tr>
            </thead>
            <tbody>
              <tr v-for="[key, p] in partRows(st.draft.report)" :key="key">
                <td>{{ p.title }}</td>
                <td class="small">{{ p.present ? p.file_name : '' }}<span v-if="p.inherited" class="muted"> (из действующей)</span></td>
                <td class="r num">{{ p.chunks ?? '—' }}</td>
                <td class="r num">{{ p.clauses ?? '—' }}</td>
                <td class="r num">{{ pct(p.refs_rate) }}</td>
                <td><span class="badge" :class="badge(p).cls">{{ badge(p).text }}</span></td>
              </tr>
            </tbody>
          </table>
          <div style="display: flex; gap: 12px; align-items: center; margin-top: 14px; flex-wrap: wrap">
            <button class="btn btn-primary" type="button" data-testid="epoz-activate" :disabled="busy || (!st.draft.report.can_activate && !force)" @click="activate">Сделать действующей</button>
            <label v-if="!st.draft.report.can_activate" class="small"><input v-model="force" type="checkbox" /> Активировать несмотря на замечания (осознанно)</label>
          </div>
        </div>
      </div>

      <div v-if="st.active" class="card" style="margin-bottom: 16px" data-testid="epoz-active">
        <div class="card-h"><h3 class="grow">Действующая редакция №{{ st.active.id }}</h3><span class="small muted">защищена от удаления</span></div>
        <table class="tbl">
          <thead><tr><th>Часть комплекта</th><th>Файл</th><th class="r">Фрагментов</th><th class="r">Пунктов</th><th>Нумерация</th><th>Статус</th></tr></thead>
          <tbody>
            <tr v-for="[key, p] in partRows(st.active.report)" :key="key">
              <td>{{ p.title }}</td>
              <td class="small">{{ p.file_name }}</td>
              <td class="r num">{{ p.chunks ?? '—' }}</td>
              <td class="r num">{{ p.clauses ?? '—' }}</td>
              <td class="small">{{ p.method === 'toc' ? 'по оглавлению' : 'по нумерации Word' }}</td>
              <td><span class="badge" :class="badge(p).cls">{{ badge(p).text }}</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card" style="margin-bottom: 16px">
        <div class="card-h"><h3 class="grow">Доступ и настройки</h3></div>
        <div class="card-b">
          <p class="small muted" style="margin-bottom: 8px">
            Группы, которым открыт <b>весь комплект</b>. Остальные не увидят ЕПоЗ ни в поиске, ни в чате, ни в просмотре.
          </p>
          <div style="display: flex; flex-wrap: wrap; gap: 6px 18px; margin-bottom: 10px">
            <label v-for="g in st.groups" :key="g.id" class="small"><input type="checkbox" :checked="picked.includes(g.id)" @change="toggleGroup(g.id)" /> {{ g.name }}</label>
            <span v-if="!st.groups.length" class="small muted">Групп пока нет: создайте их в «Пользователи и группы».</span>
          </div>
          <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="saveGroups">Сохранить доступ</button>
          <hr style="margin: 16px 0; border: 0; border-top: 1px solid var(--line)" />
          <p class="small muted" style="margin-bottom: 8px">
            Группа заказчика организации (определение — в разделе «Термины» Положения): I — закупки регулирует 223-ФЗ, II — не регулирует, но применяется Положение.
          </p>
          <div style="display: flex; gap: 12px; align-items: center">
            <select v-model="customerGroup" class="select" style="max-width: 240px" aria-label="Группа заказчика">
              <option value="">Не задана</option>
              <option value="I">I группа</option>
              <option value="II">II группа</option>
            </select>
            <button class="btn btn-secondary btn-sm" type="button" :disabled="busy" @click="saveCustomer">Сохранить</button>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-h"><h3 class="grow">Найти пункт по номеру</h3><span class="small muted">так проверяют, что нумерация восстановлена верно</span></div>
        <div class="card-b">
          <div style="display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap">
            <div class="field" style="min-width: 320px">
              <label class="label" for="cl-part">Часть комплекта</label>
              <select id="cl-part" v-model="clausePart" class="select">
                <option v-for="k in partKeys" :key="k" :value="k">{{ st.part_titles[k] }}</option>
              </select>
            </div>
            <div class="field" style="width: 140px">
              <label class="label" for="cl-label">Номер пункта</label>
              <input id="cl-label" v-model="clauseLabel" class="input" placeholder="10.3" @keyup.enter="findClause" />
            </div>
            <label v-if="st.draft" class="small" style="padding-bottom: 12px"><input v-model="clauseDraft" type="checkbox" /> в черновике</label>
            <button class="btn btn-secondary" type="button" :disabled="busy" @click="findClause">Найти</button>
          </div>
          <div v-if="clauseResult" style="margin-top: 14px" data-testid="epoz-clause">
            <div v-if="!clauseResult.found" class="alert a-warn"><AppIcon name="warn" /><div>Пункт «{{ clauseResult.label }}» не найден.</div></div>
            <div v-for="c in clauseResult.clauses" :key="c.label + c.text.slice(0, 20)" class="small" :style="{ marginBottom: '6px', paddingLeft: Math.max(0, c.level - 3) * 14 + 'px', fontWeight: c.is_heading ? 600 : 400 }">
              {{ c.text }}
            </div>
            <p v-if="clauseResult.truncated" class="small muted">Показаны первые 200 пунктов.</p>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top: 16px" data-testid="okpd-card">
        <div class="card-h"><h3 class="grow">Справочник ОКПД2</h3><span class="small muted">нужен для проверки кода (критерий 2.1)</span></div>
        <div class="card-b">
          <p v-if="okpd" class="small" style="margin-bottom: 10px">
            <b>{{ okpd.title }}</b><br />
            Кодов: {{ okpd.records }}<span v-if="okpd.effective_date"> · введён {{ okpd.effective_date }}</span><span v-if="okpd.imported_at"> · загружен {{ formatDateTime(okpd.imported_at) }}</span>
          </p>
          <div v-else class="alert a-warn" style="margin-bottom: 10px"><AppIcon name="warn" /><div>Справочник не загружен: критерий «Корректность кода ОКПД2» будет отвечать «нужна ручная проверка».</div></div>
          <label class="btn btn-secondary" style="cursor: pointer"><AppIcon name="down" />Загрузить Excel (.xlsx)<input type="file" accept=".xlsx" style="display: none" data-testid="okpd-file" @change="onOkpd" /></label>
          <div v-if="okpd" style="display: flex; gap: 12px; align-items: flex-end; margin-top: 14px; flex-wrap: wrap">
            <div class="field" style="min-width: 320px">
              <label class="label" for="okpd-q">Код или слова названия</label>
              <input id="okpd-q" v-model="okpdQuery" class="input" placeholder="26.20.11.110 или ноутбуки" @keyup.enter="findOkpd" />
            </div>
            <button class="btn btn-secondary" type="button" :disabled="busy || !okpdQuery.trim()" @click="findOkpd">Найти</button>
          </div>
          <div v-if="okpdResult" style="margin-top: 12px" data-testid="okpd-result">
            <div v-if="!okpdResult.found" class="alert a-warn"><AppIcon name="warn" /><div>Ничего не найдено.</div></div>
            <div v-for="(c, n) in okpdResult.chain ?? []" :key="c.code" class="small" :style="{ paddingLeft: n * 14 + 'px' }"><b>{{ c.code }}</b> {{ c.name }}</div>
            <div v-for="h in okpdResult.hits ?? []" :key="h.code" class="small"><b>{{ h.code }}</b> {{ h.name }}</div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.dropzone {
  display: flex;
  gap: 14px;
  align-items: center;
  padding: 18px 20px;
  border: 2px dashed var(--line-strong);
  border-radius: var(--radius);
  cursor: pointer;
  background: var(--surface-2);
}
.dropzone.on,
.dropzone:hover {
  border-color: var(--accent);
  background: #fff;
}
</style>
