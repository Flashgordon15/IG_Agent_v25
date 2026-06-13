import { useState } from "react";
import { api } from "../api/client";

/**
 * Stage 2 — secure password gate before release notes / dashboard.
 */
export default function AuthLogin({ onSuccess }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [fadeOut, setFadeOut] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.login(password);
      if (!result?.authenticated) {
        setError("Access Denied — check your administrator password.");
        return;
      }
      setFadeOut(true);
      window.setTimeout(() => onSuccess?.(), 400);
    } catch {
      setError("Access Denied — check your administrator password.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className={`auth-gate${fadeOut ? " auth-gate--fade-out" : ""}`}
      role="dialog"
      aria-labelledby="auth-gate-title"
      aria-modal="true"
    >
      <div className="auth-gate__card">
        <div className="auth-gate__brand">
          <span className="auth-gate__logo">IG</span>
          <div>
            <h1 id="auth-gate-title" className="auth-gate__title">
              Secure Access
            </h1>
            <p className="auth-gate__subtitle">Stage 1 of 3 · Administrator authentication</p>
          </div>
        </div>

        <form className="auth-gate__form" onSubmit={handleSubmit} autoComplete="off">
          <label className="auth-gate__label" htmlFor="ig-admin-password-input">
            Password
          </label>
          <input
            id="ig-admin-password-input"
            name="ig-admin-password-input"
            type="password"
            className="auth-gate__input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            spellCheck={false}
            data-1p-ignore="true"
            data-lpignore="true"
            autoFocus
            disabled={submitting || fadeOut}
            placeholder="Type your admin password"
          />

          <p className="auth-gate__hint">
            Host password is set via <code>ADMIN_PASSWORD</code> on the Mac Mini (launchd plist).
          </p>

          {error && (
            <p className="auth-gate__error" role="alert">
              {error}
            </p>
          )}

          <button
            type="submit"
            className="auth-gate__submit"
            disabled={submitting || fadeOut || !password.trim()}
          >
            {submitting ? "Verifying…" : "Unlock Dashboard"}
          </button>
        </form>
      </div>
    </div>
  );
}
