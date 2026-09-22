<script setup lang="ts">
import { RBtn, RDialog, RTextField } from "@v2/lib";
import MarkdownIt from "markdown-it";
import { computed, nextTick, ref, watch } from "vue";
import type { Turn } from "@/__generated__";
import api from "@/services/api";
import { useCan } from "@/v2/composables/useCan";

type Action = { id: string; label: string; details: string };
type Reply = { message: string; action: Action | null };

const isAdmin = useCan("app.admin");
const enabled = ref(false);
const open = ref(false);
const busy = ref(false);
const input = ref("");
const error = ref("");
const turns = ref<Turn[]>([]);
const action = ref<Action | null>(null);
const log = ref<HTMLElement | null>(null);
const canSend = computed(() => !!input.value.trim() && !busy.value);
const markdown = new MarkdownIt({ html: false, linkify: false, breaks: true });

watch(
  isAdmin,
  async (allowed) => {
    enabled.value = false;
    if (!allowed) return;
    try {
      const response = await api.get<{ enabled: boolean }>("/assistant/status");
      enabled.value = response.data.enabled;
    } catch {
      enabled.value = false;
    }
  },
  { immediate: true },
);

async function scrollToEnd() {
  await nextTick();
  log.value?.scrollTo({ top: log.value.scrollHeight });
}

async function send(text = input.value) {
  const message = text.trim();
  if (!message || busy.value) return;
  const history = turns.value.slice(-12);
  turns.value.push({ role: "user", content: message });
  input.value = "";
  action.value = null;
  error.value = "";
  busy.value = true;
  await scrollToEnd();
  try {
    const response = await api.post<Reply>("/assistant/chat", {
      message,
      history,
    });
    turns.value.push({ role: "assistant", content: response.data.message });
    action.value = response.data.action;
  } catch {
    error.value = "The assistant could not finish that request. Try again.";
  } finally {
    busy.value = false;
    await scrollToEnd();
  }
}

async function confirmAction() {
  if (!action.value || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const response = await api.post<{ job_id: string; message: string }>(
      "/assistant/confirm",
      { action_id: action.value.id },
    );
    turns.value.push({
      role: "assistant",
      content: `${response.data.message} (job ${response.data.job_id}). Ask me to check its progress.`,
    });
    action.value = null;
  } catch {
    error.value =
      "The action could not be queued. Ask again to prepare a fresh action.";
    action.value = null;
  } finally {
    busy.value = false;
    await scrollToEnd();
  }
}
</script>

<template>
  <template v-if="enabled && isAdmin">
    <RBtn
      class="r-v2-assistant__trigger"
      icon="mdi-auto-fix"
      variant="translucent"
      aria-label="Open Shardcade assistant"
      title="Shardcade assistant"
      @click="open = true"
    />
    <RDialog
      v-model="open"
      :width="620"
      :height="700"
      scroll-content
      full-height-on-mobile
    >
      <template #header>
        <div class="r-v2-assistant__heading">
          <span>Shardcade assistant</span>
          <small>Library · providers · tasks</small>
        </div>
      </template>
      <template #content>
        <div class="r-v2-assistant">
          <div
            ref="log"
            class="r-v2-assistant__log"
            role="log"
            aria-live="polite"
          >
            <div v-if="!turns.length" class="r-v2-assistant__welcome">
              <strong>What should we work on?</strong>
              <p>
                Search releases, inspect the library, or run a platform task.
              </p>
              <p>
                Your messages and relevant library results are sent to
                OpenRouter.
              </p>
              <div class="r-v2-assistant__suggestions">
                <RBtn
                  variant="outlined"
                  size="small"
                  @click="send('What provider downloads are running?')"
                  >Active downloads</RBtn
                >
                <RBtn
                  variant="outlined"
                  size="small"
                  @click="send('What maintenance tasks can you run?')"
                  >Available tasks</RBtn
                >
              </div>
            </div>
            <div
              v-for="(turn, index) in turns"
              :key="index"
              class="r-v2-assistant__message"
              :class="`r-v2-assistant__message--${turn.role}`"
            >
              <span v-if="turn.role === 'user'">{{ turn.content }}</span>
              <!-- MarkdownIt disables raw HTML and unsafe links. -->
              <!-- eslint-disable-next-line vue/no-v-html -->
              <div v-else v-html="markdown.render(turn.content)" />
            </div>
            <p v-if="busy" class="r-v2-assistant__thinking" role="status">
              Working…
            </p>
            <section
              v-if="action"
              class="r-v2-assistant__action"
              aria-label="Proposed action"
            >
              <strong>{{ action.label }}</strong>
              <p>{{ action.details }}</p>
              <div class="r-v2-assistant__actions">
                <RBtn variant="text" :disabled="busy" @click="action = null"
                  >Dismiss</RBtn
                >
                <RBtn variant="elevated" :loading="busy" @click="confirmAction"
                  >Confirm and queue</RBtn
                >
              </div>
            </section>
            <p v-if="error" class="r-v2-assistant__error" role="alert">
              {{ error }}
            </p>
          </div>
          <form class="r-v2-assistant__form" @submit.prevent="send()">
            <RTextField
              v-model="input"
              label="Ask Shardcade"
              placeholder="Find a USA 3DS release…"
              :disabled="busy"
              :hide-details="true"
              maxlength="2000"
              autocomplete="off"
            />
            <RBtn
              type="submit"
              icon="mdi-arrow-up"
              aria-label="Send message"
              :disabled="!canSend"
              :loading="busy"
            />
          </form>
        </div>
      </template>
    </RDialog>
  </template>
</template>

<style scoped>
.r-v2-assistant__trigger {
  flex: none;
}
.r-v2-assistant__heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-weight: 700;
}
.r-v2-assistant__heading small {
  color: var(--r-color-fg-muted);
  font-size: 0.72rem;
  font-weight: 500;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.r-v2-assistant {
  display: flex;
  flex-direction: column;
  min-height: 420px;
  height: 100%;
}
.r-v2-assistant__log {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 20px;
}
.r-v2-assistant__welcome {
  padding: 24px 12px;
  text-align: center;
  color: var(--r-color-fg-muted);
}
.r-v2-assistant__welcome strong {
  display: block;
  color: var(--r-color-fg);
  font-size: 1.2rem;
}
.r-v2-assistant__suggestions,
.r-v2-assistant__actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
  margin-top: 18px;
}
.r-v2-assistant__message {
  width: fit-content;
  max-width: 88%;
  padding: 11px 14px;
  margin: 0 0 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border-radius: var(--r-radius-card);
  background: var(--r-color-panel);
  border: 1px solid var(--r-color-panel-border);
}
.r-v2-assistant__message--user {
  margin-left: auto;
  background: color-mix(
    in srgb,
    var(--r-color-brand-primary) 20%,
    var(--r-color-panel)
  );
}
.r-v2-assistant__message :deep(p),
.r-v2-assistant__message :deep(ul),
.r-v2-assistant__message :deep(ol) {
  margin: 0 0 8px;
}
.r-v2-assistant__message :deep(:last-child) {
  margin-bottom: 0;
}
.r-v2-assistant__message :deep(ul),
.r-v2-assistant__message :deep(ol) {
  padding-left: 20px;
}
.r-v2-assistant__thinking {
  color: var(--r-color-fg-muted);
}
.r-v2-assistant__action {
  padding: 16px;
  border: 1px solid var(--r-color-brand-primary);
  border-radius: var(--r-radius-card);
  background: var(--r-color-panel);
}
.r-v2-assistant__action p {
  color: var(--r-color-fg-muted);
  overflow-wrap: anywhere;
}
.r-v2-assistant__form {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px;
  border-top: 1px solid var(--r-color-panel-border);
}
.r-v2-assistant__form :deep(.r-text-field) {
  flex: 1;
}
.r-v2-assistant__error {
  color: var(--r-color-status-base-danger);
}
</style>
