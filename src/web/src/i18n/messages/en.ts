import type { DeepPartial } from "../types";
import type { Messages } from "./zh-CN";

// English is a placeholder locale: only shared chrome is translated so far. Untranslated keys
// fall back to zh-CN.
const en: DeepPartial<Messages> = {
  app: { name: "Nexus Control Plane", description: "Admin console for the Nexus AI API control plane" },
  common: {
    cancel: "Cancel",
    close: "Close",
    save: "Save",
    create: "Create",
    retry: "Retry",
    reload: "Reload",
    refresh: "Refresh",
    loadMore: "Load more",
    loading: "Loading…",
    copy: "Copy",
    copied: "Copied",
    done: "Done",
  },
  errors: {
    title: "Request failed",
    traceId: "Trace ID",
    copyTraceId: "Copy trace ID",
    conflictTitle: "Someone else changed this data",
    signInAgain: "Sign in again",
  },
  login: {
    title: "Sign in",
    subtitle: "Use the account an administrator created for you. Self sign-up is not available.",
    login: "Login",
    password: "Password",
    submit: "Sign in",
    submitting: "Signing in…",
    invalid: "Incorrect login or password",
    language: "Language",
  },
  user: { signOut: "Sign out", language: "中文" },
  keys: { title: "API keys", issue: "Issue key" },
};

export default en;
