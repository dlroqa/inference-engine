import { createContext, useContext } from "react";
import { ApiError } from "../lib/api";

// Ends the signed-in dashboard session when a protected request shows that the
// saved key no longer grants operator access: 401 (invalid/revoked key, or auth
// now required) or 403 operator_role_required (a client key). The shell supplies
// one reporter per session, bound to that session: a late failure from an
// earlier session is ignored, and a burst of failures ends a session once.
//
// Feature 403s, other 403s, network errors and 5xx are not auth loss and keep
// their own handling (the reporter returns false for them).

/** Returns true when `e` ended (or belonged to an already-ended) session. */
export type AuthFailureReporter = (e: unknown) => boolean;

export function isAuthFailure(e: unknown): e is ApiError {
  return e instanceof ApiError && (e.status === 401 || (e.status === 403 && e.code === "operator_role_required"));
}

const AuthScopeContext = createContext<AuthFailureReporter>(() => false);

export const AuthScopeProvider = AuthScopeContext.Provider;

export function useAuthFailure(): AuthFailureReporter {
  return useContext(AuthScopeContext);
}
