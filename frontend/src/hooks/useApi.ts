// src/hooks/useApi.ts — generic polling hook for REST endpoints

import { useEffect, useRef, useState } from "react";

interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useApi<T>(
  url: string,
  intervalMs?: number
): UseApiResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const isMounted = useRef(true);

  const fetchData = async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (isMounted.current) {
        setData(json);
        setError(null);
      }
    } catch (e) {
      if (isMounted.current) {
        setError(e instanceof Error ? e.message : "Request failed");
      }
    } finally {
      if (isMounted.current) setLoading(false);
    }
  };

  useEffect(() => {
    isMounted.current = true;
    setLoading(true);
    fetchData();

    let timer: ReturnType<typeof setInterval> | null = null;
    if (intervalMs) {
      timer = setInterval(fetchData, intervalMs);
    }

    return () => {
      isMounted.current = false;
      if (timer) clearInterval(timer);
    };
  }, [url, intervalMs]); // eslint-disable-line react-hooks/exhaustive-deps

  return { data, loading, error, refresh: fetchData };
}
