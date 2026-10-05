import { beforeEach, expect, it, vi } from "vitest";
import api from "@/services/api";
import { cachedProviderData, providerData } from "./providers";

vi.mock("@/services/api", () => ({ default: { get: vi.fn() } }));
beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  vi.useRealTimers();
});

it("deduplicates searches, persists across reloads, isolates accounts, and expires", async () => {
  vi.mocked(api.get).mockResolvedValue({ data: { total: 7 } });
  await Promise.all([providerData(10, "/search"), providerData(10, "/search")]);
  expect(api.get).toHaveBeenCalledTimes(1);
  expect(cachedProviderData(11, "/search")).toBeUndefined();
  vi.resetModules();
  const reloaded = await import("./providers");
  expect(reloaded.cachedProviderData(10, "/search")).toEqual({ total: 7 });
  await reloaded.providerData(10, "/search", {}, true);
  expect(api.get).toHaveBeenCalledTimes(2);
  vi.spyOn(Date, "now").mockReturnValue(Date.now() + 7 * 60 * 60 * 1000);
  expect(reloaded.cachedProviderData(10, "/search")).toBeUndefined();
  vi.restoreAllMocks();
});

it("does not cache failures and works without persistent storage", async () => {
  vi.mocked(api.get).mockRejectedValueOnce(new Error("offline"));
  await expect(providerData(20, "/search")).rejects.toThrow("offline");
  expect(cachedProviderData(20, "/search")).toBeUndefined();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  vi.mocked(api.get).mockResolvedValue({ data: [] });
  expect(await providerData(20, "/search")).toEqual([]);
  expect(cachedProviderData(20, "/search")).toEqual([]);
  vi.restoreAllMocks();
});

it("bounds persistent entries and storage size", async () => {
  vi.mocked(api.get).mockResolvedValue({ data: "x".repeat(30_000) });
  for (let page = 0; page < 90; page++)
    await providerData(30, "/bounded", { page });
  const keys = Object.keys(localStorage).filter((name) =>
    name.startsWith("shardcade:providers:"),
  );
  expect(keys.length).toBeLessThanOrEqual(80);
  expect(
    keys.reduce(
      (bytes, name) => bytes + localStorage.getItem(name)!.length * 2,
      0,
    ),
  ).toBeLessThanOrEqual(3 * 1024 * 1024);
});
