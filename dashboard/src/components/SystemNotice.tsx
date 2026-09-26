import type { JSX } from "react";
import { Icon } from "./Icon";
import { WiredTo } from "./WiredTo";
import { useSystem } from "../hooks/useSystem";
import type { SwitchName } from "../lib/api";
import { switchReason } from "../lib/switches";

// A view-level explanation of feature-switch state for the actions on a page:
// which relevant switches are off (and what that disables), or that the state
// is unavailable/out of date, with Retry. Row controls point at it with
// aria-describedby (`id`), so one notice explains every disabled row action.
//
// Unknown state is stated as unknown: actions stay available and the engine
// enforces the switch; it is never presented as "enabled".
export function SystemNotice({
  id,
  switches,
  consequence,
}: {
  id: string;
  switches: readonly SwitchName[];
  /** What the off switches make unavailable on this page. */
  consequence: string;
}): JSX.Element | null {
  const sys = useSystem();
  if (sys.status === "absent") return null;

  const retry = (
    <>
      <button className="linkbtn" type="button" onClick={sys.refresh}>
        <Icon name="refresh" size={14} /> Retry
      </button>
      <WiredTo id="app.system" />
    </>
  );

  // Includes switches the engine has refused since the last successful fetch.
  const off = sys.offSwitches(switches);

  if (sys.status === "error") {
    return (
      <div className="banner info switch-notice" id={id} role="status" data-wiring="app.system">
        {off.length > 0 && <Icon name="lock" size={16} />}
        <span>
          {off.length > 0 && `${switchReason(off)} ${consequence} `}
          Feature-switch status unavailable ({sys.error}). {off.length > 0 ? "Other actions" : "Actions"} stay
          available; the engine still refuses any action a switch disables.
        </span>
        {retry}
      </div>
    );
  }

  // While loading, only a switch the engine has already refused is shown, so a
  // disabled control's aria-describedby always has a target.
  if (off.length === 0 && (sys.status === "loading" || !sys.stale)) return null;
  return (
    <div className="banner info switch-notice" id={id} role="status" data-wiring="app.system">
      {off.length > 0 && <Icon name="lock" size={16} />}
      <span>
        {off.length > 0 && `${switchReason(off)} ${consequence} `}
        {sys.stale && "Switch status could not be refreshed; this is the last known state."}
      </span>
      {sys.stale ? retry : <WiredTo id="app.system" />}
    </div>
  );
}
