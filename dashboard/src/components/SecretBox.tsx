import { useState, type JSX, type ReactNode } from "react";
import { Icon } from "./Icon";
import { WiredTo } from "./WiredTo";

// A value the engine returns exactly once (a client key's token, a webhook
// signing secret). It is shown until the operator dismisses it or leaves, and
// is never written to storage, logs, or the page address.
export function SecretBox({
  title,
  value,
  testId,
  onDismiss,
  children,
}: {
  title: string;
  value: string;
  testId: string;
  onDismiss: () => void;
  children?: ReactNode;
}): JSX.Element {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable: the value stays selectable on screen */
    }
  };
  return (
    <div className="banner info" role="status" style={{ marginTop: 12 }}>
      <strong>{title} It is shown only once: save it now.</strong>
      {children}
      <div className="token-box" style={{ marginTop: 8 }} data-testid={testId}>
        {value}
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <button className="btn" type="button" onClick={copy} data-wiring="local.copy-secret">
          <Icon name={copied ? "check" : "copy"} size={16} />
          {copied ? "Copied" : "Copy"}
        </button>
        <WiredTo id="local.copy-secret" />
        <button className="btn" type="button" onClick={onDismiss} data-wiring="local.dismiss-secret">
          I have saved it
        </button>
        <WiredTo id="local.dismiss-secret" />
      </div>
    </div>
  );
}
