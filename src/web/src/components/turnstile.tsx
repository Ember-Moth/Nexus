"use client";

import Script from "next/script";
import { useCallback, useEffect, useRef, useState } from "react";

// Cloudflare Turnstile, rendered explicitly. Only mounted when GET /auth/options reports
// `turnstile: true`; every protected request needs a fresh token, so callers reset the widget
// after each submit. https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/

interface TurnstileApi {
  render: (element: HTMLElement, options: Record<string, unknown>) => string;
  reset: (widgetId: string) => void;
  remove: (widgetId: string) => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

const SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

export function useTurnstile() {
  const [token, setToken] = useState<string | null>(null);
  const widget = useRef<string | null>(null);
  const reset = useCallback(() => {
    setToken(null);
    if (widget.current && window.turnstile) window.turnstile.reset(widget.current);
  }, []);
  return { token, setToken, widget, reset };
}

export function Turnstile({
  siteKey,
  action,
  onToken,
  widgetRef,
}: {
  siteKey: string;
  /** Must match the backend action for the endpoint, e.g. "login" or "register". */
  action: string;
  onToken: (token: string | null) => void;
  widgetRef: React.RefObject<string | null>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [loaded, setLoaded] = useState(() => typeof window !== "undefined" && Boolean(window.turnstile));

  useEffect(() => {
    if (!loaded || !container.current || !window.turnstile || widgetRef.current) return;
    widgetRef.current = window.turnstile.render(container.current, {
      sitekey: siteKey,
      action,
      callback: (value: string) => onToken(value),
      "expired-callback": () => onToken(null),
      "error-callback": () => onToken(null),
    });
    return () => {
      if (widgetRef.current && window.turnstile) window.turnstile.remove(widgetRef.current);
      widgetRef.current = null;
    };
  }, [loaded, siteKey, action, onToken, widgetRef]);

  return (
    <>
      <Script src={SCRIPT} strategy="afterInteractive" onReady={() => setLoaded(true)} />
      <div ref={container} className="min-h-[65px]" />
    </>
  );
}
