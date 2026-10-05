import api from "@/services/api";

const PREFIX = "shardcade:providers:v1:";
const TTL = 6 * 60 * 60 * 1000;
const MAX_ENTRIES = 80;
const MAX_STORAGE_BYTES = 3 * 1024 * 1024;
type Entry = { expires: number; data: unknown };
const memory = new Map<string, Entry>();
const pending = new Map<string, Promise<unknown>>();

function key(
  user: number,
  path: string,
  params: Record<string, string | number>,
) {
  const query = new URLSearchParams(
    Object.entries(params)
      .sort()
      .map(([name, value]) => [name, String(value)]),
  );
  return `${PREFIX}${user}:${path}?${query}`;
}

export function cachedProviderData<T>(
  user: number,
  path: string,
  params: Record<string, string | number> = {},
): T | undefined {
  const id = key(user, path, params);
  let entry = memory.get(id);
  try {
    if (!entry) {
      const raw = localStorage.getItem(id);
      if (raw) entry = JSON.parse(raw) as Entry;
    }
  } catch {
    /* Storage may be unavailable in private browsing. */
  }
  if (entry && entry.expires > Date.now()) return entry.data as T;
  memory.delete(id);
  try {
    localStorage.removeItem(id);
  } catch {
    /* Memory fallback. */
  }
}

function store(id: string, data: unknown) {
  const entry: Entry = { expires: Date.now() + TTL, data };
  const serialized = JSON.stringify(entry);
  memory.delete(id);
  memory.set(id, entry);
  while (memory.size > MAX_ENTRIES) memory.delete(memory.keys().next().value!);
  try {
    const entries = Object.keys(localStorage).filter(
      (name) => name.startsWith(PREFIX) && name !== id,
    );
    const ordered = entries
      .map((name) => {
        const raw = localStorage.getItem(name) || "{}";
        return {
          name,
          bytes: raw.length * 2,
          expires: (JSON.parse(raw) as Entry).expires || 0,
        };
      })
      .sort((a, b) => a.expires - b.expires);
    let count = ordered.length;
    let bytes =
      serialized.length * 2 +
      ordered.reduce((total, item) => total + item.bytes, 0);
    for (const item of ordered) {
      if (
        item.expires <= Date.now() ||
        count >= MAX_ENTRIES ||
        bytes > MAX_STORAGE_BYTES
      ) {
        localStorage.removeItem(item.name);
        count--;
        bytes -= item.bytes;
      }
    }
    if (serialized.length * 2 <= MAX_STORAGE_BYTES)
      localStorage.setItem(id, serialized);
  } catch {
    /* Quota errors must not prevent a search. */
  }
}

export function rememberProviderView(
  user: number,
  view: Record<string, string | number>,
) {
  store(key(user, "/view", {}), view);
}

export async function providerData<T>(
  user: number,
  path: string,
  params: Record<string, string | number> = {},
  force = false,
): Promise<T> {
  const id = key(user, path, params);
  if (!force) {
    const cached = cachedProviderData<T>(user, path, params);
    if (cached !== undefined) return cached;
  }
  let request = pending.get(id);
  if (!request) {
    request = api
      .get<T>(path, { params })
      .then(({ data }) => {
        store(id, data);
        return data;
      })
      .finally(() => pending.delete(id));
    pending.set(id, request);
  }
  return request as Promise<T>;
}
