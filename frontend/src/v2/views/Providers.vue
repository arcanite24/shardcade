<script setup lang="ts">
import {
  RAlert,
  RBtn,
  RDialog,
  RProgressLinear,
  RSelect,
  RTextField,
} from "@v2/lib";
import { useIntervalFn } from "@vueuse/core";
import { isAxiosError } from "axios";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";
import type {
  ProviderJob,
  ProviderMegaFile,
  ProviderResult,
  ProviderSearch,
  ProviderStatus,
} from "@/__generated__";
import { ROUTES } from "@/plugins/router";
import api from "@/services/api";
import storePlatforms from "@/stores/platforms";
import { formatBytes, formatTimestamp, toBrowserLocale } from "@/utils";
import { useCan } from "@/v2/composables/useCan";
import { useSnackbar } from "@/v2/composables/useSnackbar";

const { t, locale } = useI18n();
const numberFormat = computed(
  () => new Intl.NumberFormat(toBrowserLocale(locale.value)),
);
const n = (value: number) => numberFormat.value.format(value);
const route = useRoute();
const router = useRouter();
const snackbar = useSnackbar();
const canManage = useCan("app.admin");
const platforms = storePlatforms();
const providers = ["minerva", "axekin", "vimm", "edgeemu", "startgame"].map(
  (id) => ({
    id,
    name: {
      minerva: "Minerva",
      axekin: "Axekin",
      vimm: "Vimm's Lair",
      edgeemu: "Edge Emulation",
      startgame: "StartGame",
    }[id],
  }),
);
const provider = ref(String(route.query.provider || "minerva"));
const supportsFilters = computed(
  () => !["axekin", "startgame"].includes(provider.value),
);
const query = ref(String(route.query.query || ""));
const platform = ref(String(route.query.platform || ""));
const page = ref(Math.max(1, Number(route.query.page) || 1));
const tab = ref(route.query.tab === "downloads" ? "downloads" : "search");
const state = ref<ProviderStatus>();
const results = ref<ProviderSearch>();
const error = ref("");
const statusError = ref("");
const loading = ref(false);
const busy = ref(false);
const selected = ref<ProviderResult>();
const dialog = ref(false);
const option = ref(0);
const destination = ref<number>();
const verifiedUrl = ref("");
const megaFiles = ref<ProviderMegaFile[]>([]);
const megaNode = ref<string>();
const filesLoading = ref(false);
const dialogError = ref("");
const selectedOption = computed(() => selected.value?.options?.[option.value]);
const isMega = computed(
  () =>
    selectedOption.value?.method === "mega" ||
    /^https:\/\/mega\.(?:nz|co\.nz)\/(?:folder|file)\/[\w-]{8}#[\w-]{22}(?:[\w-]{21})?$/.test(
      verifiedUrl.value,
    ),
);
const indexBusy = computed(() =>
  state.value?.jobs?.some((job) => job.kind === "index" && active(job)),
);
const platformOptions = computed(() => [
  { id: "", name: t("common.all-platforms") },
  ...(state.value?.platforms || []).map((slug) => ({
    id: slug,
    name:
      platforms.allPlatforms.find((p) => p.slug === slug)?.display_name || slug,
  })),
]);
let controller: AbortController | undefined;
let filesController: AbortController | undefined;
let alive = true;

function message(cause: unknown) {
  return isAxiosError(cause) && typeof cause.response?.data?.detail === "string"
    ? cause.response.data.detail
    : t("providers.failed");
}
function active(job: ProviderJob) {
  return ["queued", "started", "deferred"].includes(job.state);
}
function size(bytes?: number | null) {
  return bytes == null ? t("providers.unknown-size") : formatBytes(bytes, 1);
}
async function loadStatus() {
  if (!alive || !canManage.value) return;
  try {
    const { data } = await api.get<ProviderStatus>("/providers/status");
    if (alive) {
      state.value = data;
      statusError.value = "";
    }
  } catch (cause) {
    if (alive) statusError.value = message(cause);
  }
}
async function saveQuery() {
  await router.replace({
    query: {
      provider: provider.value,
      query: query.value || undefined,
      platform: platform.value || undefined,
      page: page.value,
      tab: tab.value,
    },
  });
}
async function search(reset = true) {
  if (reset) page.value = 1;
  controller?.abort();
  const request = new AbortController();
  controller = request;
  loading.value = true;
  error.value = "";
  results.value = undefined;
  await saveQuery();
  try {
    const { data } = await api.get<ProviderSearch>("/providers/search", {
      signal: request.signal,
      params: {
        provider: provider.value,
        query: query.value,
        platform: supportsFilters.value ? platform.value : "",
        page: page.value,
        limit: 20,
      },
    });
    if (!request.signal.aborted && alive) results.value = data;
  } catch (cause) {
    if (!request.signal.aborted && alive) error.value = message(cause);
  } finally {
    if (!request.signal.aborted && alive) loading.value = false;
  }
}
async function action(path: string) {
  busy.value = true;
  try {
    await api.post(path);
    await loadStatus();
  } catch (cause) {
    snackbar.error(message(cause));
  } finally {
    if (alive) busy.value = false;
  }
}
function choose(item: ProviderResult) {
  selected.value = item;
  option.value = 0;
  destination.value = platforms.allPlatforms.find(
    (p) => p.slug === item.platform,
  )?.id;
  verifiedUrl.value = "";
  megaNode.value = undefined;
  dialogError.value = "";
  dialog.value = true;
}
watch([selected, option], () => {
  verifiedUrl.value = "";
});
watch([selected, option, verifiedUrl], async () => {
  filesController?.abort();
  megaFiles.value = [];
  megaNode.value = undefined;
  if (!isMega.value) {
    filesLoading.value = false;
    return;
  }
  const request = new AbortController();
  filesController = request;
  filesLoading.value = true;
  try {
    const { data } = await api.post<ProviderMegaFile[]>(
      "/providers/files",
      {
        result_id: selected.value?.id,
        option: option.value,
        verified_url: verifiedUrl.value || undefined,
      },
      { signal: request.signal },
    );
    if (!request.signal.aborted && alive) megaFiles.value = data;
  } catch (cause) {
    if (!request.signal.aborted && alive) dialogError.value = message(cause);
  } finally {
    if (!request.signal.aborted && alive) filesLoading.value = false;
  }
});
async function download() {
  busy.value = true;
  dialogError.value = "";
  try {
    await api.post("/providers/downloads", {
      result_id: selected.value?.id,
      option: option.value,
      platform_id: destination.value,
      verified_url: verifiedUrl.value || undefined,
      mega_node_id: megaNode.value,
    });
    dialog.value = false;
    tab.value = "downloads";
    await saveQuery();
    await loadStatus();
    snackbar.success(t("providers.queued"));
  } catch (cause) {
    dialogError.value = message(cause);
  } finally {
    if (alive) busy.value = false;
  }
}
watch(tab, saveQuery);
watch(provider, () => {
  controller?.abort();
  results.value = undefined;
  loading.value = false;
  platform.value = "";
  page.value = 1;
});
watch(canManage, (allowed) => {
  if (allowed) void loadStatus();
});
useIntervalFn(() => {
  if (!document.hidden) void loadStatus();
}, 5000);
onMounted(async () => {
  await Promise.all([loadStatus(), platforms.fetchPlatforms()]);
  if (query.value && state.value?.enabled) await search(false);
});
onBeforeUnmount(() => {
  alive = false;
  controller?.abort();
  filesController?.abort();
});
</script>

<template>
  <main class="providers">
    <header class="providers__header">
      <div>
        <h1>{{ t("providers.title") }}</h1>
        <p>{{ t("providers.intro") }}</p>
      </div>
      <RBtn
        :to="{ name: ROUTES.UPLOAD }"
        variant="text"
        prepend-icon="mdi-cloud-upload-outline"
        >{{ t("common.upload-roms") }}</RBtn
      >
    </header>
    <RAlert v-if="!canManage" type="warning">{{
      t("providers.admin-only")
    }}</RAlert>
    <template v-else>
      <RAlert v-if="statusError" type="error"
        >{{ statusError }}
        <RBtn variant="text" @click="loadStatus">{{
          t("providers.retry")
        }}</RBtn></RAlert
      >
      <RAlert v-if="state && !state.enabled" type="info">{{
        t("providers.disabled")
      }}</RAlert>
      <section class="providers__index" :aria-label="t('providers.index')">
        <div>
          <h2>{{ t("providers.index") }}</h2>
          <p v-if="state?.index_ready">
            {{ t("providers.index-ready", { count: n(state.records || 0) }) }}
            <span v-if="state.indexed_at">{{
              formatTimestamp(state.indexed_at, locale)
            }}</span>
          </p>
          <p v-else>{{ t("providers.index-empty") }}</p>
        </div>
        <RBtn
          :disabled="!state?.enabled || indexBusy || busy"
          prepend-icon="mdi-database-sync-outline"
          @click="action('/providers/index')"
          >{{
            indexBusy ? t("providers.refreshing") : t("providers.refresh")
          }}</RBtn
        >
      </section>
      <nav class="d-flex ga-2 mb-6" :aria-label="t('providers.title')">
        <RBtn
          :variant="tab === 'search' ? 'translucent' : 'text'"
          :aria-pressed="tab === 'search'"
          @click="tab = 'search'"
          >{{ t("common.search") }}</RBtn
        >
        <RBtn
          :variant="tab === 'downloads' ? 'translucent' : 'text'"
          :aria-pressed="tab === 'downloads'"
          @click="tab = 'downloads'"
          >{{ t("providers.downloads") }}</RBtn
        >
      </nav>
      <section v-if="tab === 'search'" :aria-label="t('common.search')">
        <form class="providers__search" @submit.prevent="search()">
          <RSelect
            v-model="provider"
            :items="providers"
            item-title="name"
            item-value="id"
            :label="t('providers.provider')"
            hide-details
          />
          <RTextField
            v-model="query"
            :label="t('common.search')"
            :placeholder="t('providers.search-placeholder')"
            hide-details
          />
          <RSelect
            v-model="platform"
            :disabled="!supportsFilters"
            :items="platformOptions"
            item-title="name"
            item-value="id"
            :label="t('common.platform')"
            searchable
            hide-details
          />
          <RBtn
            type="submit"
            :loading="loading"
            :disabled="
              !state?.enabled || (provider === 'minerva' && !state.index_ready)
            "
            >{{ t("common.search") }}</RBtn
          >
        </form>
        <RAlert v-if="error" type="error" class="mt-4">{{ error }}</RAlert>
        <p v-if="!results && !loading && !error" class="providers__empty">
          {{ t("providers.search-hint") }}
        </p>
        <p v-if="results?.items.length === 0" class="providers__empty">
          {{ t("providers.no-results") }}
        </p>
        <RProgressLinear
          v-if="loading"
          indeterminate
          :aria-label="t('common.loading')"
          class="mt-4"
        />
        <div v-if="results" aria-live="polite">
          <p class="mt-4">
            {{ t("providers.results", { count: n(results.total) }) }}
          </p>
          <article
            v-for="item in results.items"
            :key="item.id"
            class="providers__result"
          >
            <div class="providers__description">
              <h3>{{ item.name }}</h3>
              <p>
                {{ item.platform || t("providers.unknown-platform") }} ·
                {{ item.region }} · {{ size(item.size) }}
              </p>
              <p v-if="item.filename" class="providers__filename">
                {{ item.filename }}
              </p>
              <p v-if="item.collection" class="providers__filename">
                {{ item.collection }}
              </p>
              <a
                :href="item.source_url"
                target="_blank"
                rel="noopener noreferrer"
                >{{ t("providers.source") }}</a
              >
            </div>
            <RBtn
              :disabled="!item.options?.length"
              prepend-icon="mdi-download-outline"
              @click="choose(item)"
              >{{ t("providers.import") }}</RBtn
            >
          </article>
          <nav
            class="d-flex align-center justify-space-between mt-4"
            :aria-label="t('common.search')"
          >
            <RBtn
              :disabled="page <= 1 || loading"
              variant="text"
              @click="
                page--;
                search(false);
              "
              >{{ t("common.previous-page") }}</RBtn
            >
            <span>{{ n(page) }}</span>
            <RBtn
              :disabled="page * results.limit >= results.total || loading"
              variant="text"
              @click="
                page++;
                search(false);
              "
              >{{ t("common.next-page") }}</RBtn
            >
          </nav>
        </div>
      </section>
      <section v-else :aria-label="t('providers.downloads')">
        <p v-if="!state?.jobs?.length" class="providers__empty">
          {{ t("providers.no-downloads") }}
        </p>
        <article
          v-for="job in state?.jobs"
          :key="job.id"
          class="providers__job"
        >
          <div class="d-flex align-center justify-space-between ga-4">
            <div>
              <h3>{{ job.name }}</h3>
              <p>
                {{
                  t(
                    `providers.phase-${job.state === "failed" ? "failed" : job.phase}`,
                  )
                }}<span v-if="job.total_bytes">
                  · {{ size(job.completed_bytes) }} /
                  {{ size(job.total_bytes) }}</span
                ><span v-if="job.records"> · {{ n(job.records) }}</span>
              </p>
            </div>
            <RBtn
              v-if="active(job)"
              variant="text"
              :disabled="busy"
              @click="action(`/providers/jobs/${job.id}/cancel`)"
              >{{ t("common.cancel") }}</RBtn
            >
            <RBtn
              v-else-if="['failed', 'cancelled', 'stopped'].includes(job.state)"
              variant="text"
              :disabled="busy"
              @click="action(`/providers/jobs/${job.id}/retry`)"
              >{{ t("providers.retry") }}</RBtn
            >
            <RBtn
              v-if="job.rom_id"
              :to="{ name: ROUTES.ROM, params: { rom: job.rom_id } }"
              variant="text"
              >{{ t("providers.open-rom") }}</RBtn
            >
          </div>
          <RProgressLinear
            v-if="active(job)"
            :model-value="(job.progress || 0) * 100"
            :indeterminate="!job.progress"
            :aria-label="job.name"
          />
          <RAlert v-if="job.error" type="error" class="mt-3">{{
            job.error
          }}</RAlert>
        </article>
      </section>
    </template>
    <RDialog v-model="dialog" :width="620" scroll-content>
      <template #header
        ><h2>{{ t("providers.import") }}</h2></template
      >
      <template #content>
        <div class="pa-4 d-flex flex-column ga-4">
          <h3>{{ selected?.name }}</h3>
          <RSelect
            v-model="option"
            :items="
              selected?.options?.map((o, index) => ({
                id: index,
                name: o.label,
              }))
            "
            item-title="name"
            item-value="id"
            :label="t('providers.download-option')"
          />
          <RSelect
            v-model="destination"
            :items="platforms.allPlatforms"
            item-title="display_name"
            item-value="id"
            :label="t('providers.destination')"
            searchable
          />
          <RAlert
            v-if="
              selectedOption?.method === 'torrent' && !state?.torrent_configured
            "
            type="warning"
            >{{ t("providers.torrent-disabled") }}</RAlert
          >
          <template v-if="selectedOption?.method === 'verify'">
            <p>{{ t("providers.verification") }}</p>
            <RBtn
              :href="selectedOption.url"
              target="_blank"
              rel="noopener noreferrer"
              variant="translucent"
              >{{ t("providers.open-provider") }}</RBtn
            >
            <RTextField
              v-model="verifiedUrl"
              type="url"
              :label="t('providers.generated-url')"
            />
          </template>
          <RSelect
            v-if="isMega"
            v-model="megaNode"
            :items="megaFiles"
            item-title="name"
            item-value="id"
            :label="t('providers.choose-file')"
            :loading="filesLoading"
            searchable
          />
          <p>{{ t("providers.import-hint") }}</p>
          <RAlert v-if="dialogError" type="error">{{ dialogError }}</RAlert>
        </div>
      </template>
      <template #footer
        ><div class="d-flex justify-end ga-2">
          <RBtn variant="text" @click="dialog = false">{{
            t("common.cancel")
          }}</RBtn
          ><RBtn
            :loading="busy"
            :disabled="
              !destination ||
              !selectedOption ||
              (selectedOption.method === 'verify' && !verifiedUrl) ||
              (isMega && !megaNode) ||
              (selectedOption.method === 'torrent' &&
                !state?.torrent_configured)
            "
            @click="download"
            >{{ t("providers.import") }}</RBtn
          >
        </div></template
      >
    </RDialog>
  </main>
</template>

<style scoped>
.providers {
  max-width: 1200px;
  min-width: 0;
  margin: 0 auto;
  padding: 24px;
  color: var(--r-color-fg);
}
.providers__header,
.providers__index,
.providers__result {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.providers__header {
  margin-bottom: 32px;
}
.providers h1 {
  font-size: 1.75rem;
  font-weight: 700;
}
.providers h2 {
  font-size: 1.125rem;
  font-weight: 600;
}
.providers h3 {
  font-size: 1rem;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.providers p {
  color: var(--r-color-fg-secondary);
  margin-top: 6px;
  line-height: 1.6;
}
.providers__index {
  padding: 20px 0;
  margin-bottom: 24px;
  border-block: 1px solid var(--r-color-border);
}
.providers__search {
  display: grid;
  grid-template-columns: 170px minmax(180px, 1fr) 180px auto;
  align-items: end;
  gap: 12px;
}
.providers__result,
.providers__job {
  padding: 20px 0;
  border-bottom: 1px solid var(--r-color-border);
}
.providers__description {
  min-width: 0;
}
.providers__filename {
  overflow-wrap: anywhere;
  font-size: 0.875rem;
}
.providers a {
  color: var(--r-color-fg);
  text-underline-offset: 3px;
}
.providers__empty {
  padding: 56px 0;
  text-align: center;
}
html[data-bp~="md-and-down"] .providers__search {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
html[data-bp~="sm-and-down"] .providers {
  padding: 16px;
}
html[data-bp~="sm-and-down"] .providers__header,
html[data-bp~="sm-and-down"] .providers__index {
  align-items: flex-start;
  flex-direction: column;
  gap: 12px;
}
html[data-bp~="sm-and-down"] .providers__search {
  grid-template-columns: 1fr;
}
html[data-bp~="sm-and-down"] .providers__result {
  flex-wrap: wrap;
  gap: 12px;
}
</style>
