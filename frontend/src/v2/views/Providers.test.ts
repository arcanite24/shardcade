/* eslint-disable vue/one-component-per-file */
import { flushPromises, mount } from "@vue/test-utils";
import { expect, it, vi } from "vitest";
import { defineComponent, ref } from "vue";
import { cachedProviderData, providerData } from "@/services/api/providers";
import Providers from "./Providers.vue";

vi.mock("vue-i18n", () => ({
  useI18n: () => ({ t: (key: string) => key, locale: ref("en_US") }),
}));
vi.mock("vue-router", () => ({
  useRoute: () => ({ query: { provider: "romstime", query: "game" } }),
  useRouter: () => ({ replace: vi.fn().mockResolvedValue(undefined) }),
}));
vi.mock("@/plugins/router", () => ({
  ROUTES: { UPLOAD: "upload", ROM: "rom" },
}));
vi.mock("@/services/api", () => ({ default: { post: vi.fn() } }));
vi.mock("@/services/api/providers", () => ({
  cachedProviderData: vi.fn(),
  providerData: vi.fn(),
  rememberProviderView: vi.fn(),
}));
vi.mock("@/stores/auth", () => ({ default: () => ({ user: { id: 7 } }) }));
vi.mock("@/stores/platforms", () => ({
  default: () => ({
    allPlatforms: [{ id: 1, slug: "3ds", display_name: "3DS" }],
    fetchPlatforms: vi.fn(),
  }),
}));
vi.mock("@/utils", () => ({
  formatBytes: String,
  formatTimestamp: String,
  toBrowserLocale: () => "en-US",
}));
vi.mock("@/v2/composables/useCan", () => ({ useCan: () => ref(true) }));
vi.mock("@/v2/composables/useSnackbar", () => ({
  useSnackbar: () => ({ error: vi.fn(), success: vi.fn() }),
}));
vi.mock("@v2/lib", () => ({
  RAlert: defineComponent({ template: "<div><slot /></div>" }),
  RBtn: defineComponent({ template: "<button><slot /></button>" }),
  RDialog: defineComponent({
    props: ["modelValue"],
    template:
      '<div v-if="modelValue"><slot name="header"/><slot name="content"/><slot name="footer"/></div>',
  }),
  RProgressLinear: defineComponent({ template: "<progress />" }),
  RTextField: defineComponent({
    props: ["modelValue", "label"],
    template: '<input :aria-label="label" :value="modelValue" />',
  }),
  RSelect: defineComponent({
    props: ["items", "itemTitle", "itemValue", "label", "modelValue"],
    emits: ["update:modelValue"],
    template:
      '<select :aria-label="label" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="item in items" :key="item[itemValue]" :value="item[itemValue]">{{ item[itemTitle] }}</option></select>',
  }),
}));

it("starts a deep-linked search without waiting for status and loads RomsTime versions on demand", async () => {
  const state = {
    enabled: true,
    torrent_configured: true,
    index_ready: true,
    jobs: [],
    platforms: ["3ds"],
  };
  const item = {
    id: "romstime:game",
    provider: "romstime",
    name: "Game",
    platform: "3ds",
    region: "USA",
    source_url: "https://romstime.com/download/game",
    options: [],
  };
  vi.mocked(cachedProviderData).mockReturnValue(state);
  vi.mocked(providerData).mockImplementation(async (_user, path) => {
    if (path === "/providers/status") return new Promise(() => {});
    if (path === "/providers/search")
      return { items: [item], total: 1, limit: 24, page: 1 };
    return {
      ...item,
      options: [
        {
          label: "USA · 3DS · decrypted",
          method: "http",
          url: "https://romstime.com/api/roms/game/download",
        },
      ],
    };
  });
  const wrapper = mount(Providers);
  await flushPromises();
  expect(wrapper.text()).toContain("Game");
  expect(providerData).toHaveBeenCalledTimes(2);
  expect(providerData).not.toHaveBeenCalledWith(
    7,
    "/providers/results/romstime:game",
  );
  await wrapper.find("article button").trigger("click");
  await flushPromises();
  expect(
    wrapper.find('select[aria-label="providers.download-option"]').text(),
  ).toContain("USA · 3DS · decrypted");
  await wrapper
    .find('select[aria-label="providers.provider"]')
    .setValue("axekin");
  await flushPromises();
  expect(providerData).toHaveBeenLastCalledWith(
    7,
    "/providers/search",
    expect.objectContaining({ provider: "axekin", platform: "" }),
    false,
  );
  wrapper.unmount();
});
