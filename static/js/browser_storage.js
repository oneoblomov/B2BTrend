(function () {
  const DB_NAME = "b2btrend-browser-storage";
  const DB_VERSION = 1;
  const STORE_NAME = "kv";
  const CLIENT_ID_KEY = "b2btrend-client-id";

  const memoryStore = new Map();

  function generateClientId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return `client-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function readStoredClientId() {
    try {
      const stored = window.localStorage?.getItem(CLIENT_ID_KEY);
      if (stored && stored.trim()) {
        return stored.trim();
      }
    } catch (_error) {
      // fall through to cookie lookup
    }

    const cookieMatch = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith(`${CLIENT_ID_KEY}=`));
    if (cookieMatch) {
      const value = cookieMatch.slice(CLIENT_ID_KEY.length + 1);
      if (value.trim()) {
        return decodeURIComponent(value.trim());
      }
    }

    return "";
  }

  function persistClientId(clientId) {
    try {
      window.localStorage?.setItem(CLIENT_ID_KEY, clientId);
    } catch (_error) {
      // ignore localStorage failures; the cookie still scopes requests
    }
    document.cookie = `${CLIENT_ID_KEY}=${encodeURIComponent(clientId)}; path=/; max-age=31536000; samesite=lax`;
  }

  const clientId = readStoredClientId() || generateClientId();
  persistClientId(clientId);

  function openDatabase() {
    return new Promise((resolve) => {
      if (!window.indexedDB) {
        resolve(null);
        return;
      }

      const request = window.indexedDB.open(DB_NAME, DB_VERSION);

      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE_NAME)) {
          db.createObjectStore(STORE_NAME);
        }
      };

      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);
    });
  }

  async function withStore(mode, callback) {
    const db = await openDatabase();
    if (!db) {
      return callback({
        get: async (key) => (memoryStore.has(key) ? structuredClone(memoryStore.get(key)) : undefined),
        set: async (key, value) => {
          memoryStore.set(key, structuredClone(value));
        },
        delete: async (key) => {
          memoryStore.delete(key);
        },
        getAll: async () => Array.from(memoryStore.entries()).map(([key, value]) => [key, structuredClone(value)]),
      });
    }

    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, mode);
      const store = tx.objectStore(STORE_NAME);
      tx.oncomplete = () => {
        db.close();
      };
      tx.onerror = () => {
        db.close();
        reject(tx.error || new Error("IndexedDB transaction failed"));
      };
      Promise.resolve(callback(store))
        .then((result) => resolve(result))
        .catch((error) => reject(error));
    });
  }

  async function get(key) {
    return withStore("readonly", (store) => new Promise((resolve, reject) => {
      const request = store.get(key);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("IndexedDB get failed"));
    }));
  }

  async function set(key, value) {
    return withStore("readwrite", (store) => new Promise((resolve, reject) => {
      const request = store.put(structuredClone(value), key);
      request.onsuccess = () => resolve(true);
      request.onerror = () => reject(request.error || new Error("IndexedDB set failed"));
    }));
  }

  async function remove(key) {
    return withStore("readwrite", (store) => new Promise((resolve, reject) => {
      const request = store.delete(key);
      request.onsuccess = () => resolve(true);
      request.onerror = () => reject(request.error || new Error("IndexedDB delete failed"));
    }));
  }

  async function getAll() {
    return withStore("readonly", (store) => new Promise((resolve, reject) => {
      const request = store.getAllKeys ? store.getAllKeys() : null;
      if (!request) {
        resolve(Array.from(memoryStore.entries()));
        return;
      }
      request.onsuccess = () => {
        const keys = request.result || [];
        const valuesRequest = store.getAll();
        valuesRequest.onsuccess = () => {
          const values = valuesRequest.result || [];
          resolve(keys.map((key, index) => [key, values[index]]));
        };
        valuesRequest.onerror = () => reject(valuesRequest.error || new Error("IndexedDB getAll failed"));
      };
      request.onerror = () => reject(request.error || new Error("IndexedDB getAllKeys failed"));
    }));
  }

  window.B2BTrendStorage = {
    ready: openDatabase(),
    get,
    set,
    delete: remove,
    getAll,
    clientId,
  };
})();